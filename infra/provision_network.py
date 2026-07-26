"""Provision a fresh, dedicated VPC + private networking for the MNRE chatbot
(ap-south-1). Creates everything the private data tier needs, with NO NAT and NO
internet gateway — the private subnets reach AWS services only via VPC endpoints.

Creates (idempotently, matched by tag/name so re-runs are safe):
  - a dedicated VPC (10.20.0.0/16) tagged for this project
  - 2 PRIVATE subnets (no IGW route) across ap-south-1a / ap-south-1b for Aurora
    + the in-VPC Tool_Lambda + the Secrets Manager VPC endpoint
  - a private route table associated with both subnets (no 0.0.0.0/0 route)
  - security groups: aurora-sg, lambda-sg, vpce-sg with the design's rules
  - the Secrets Manager INTERFACE endpoint + the S3 GATEWAY endpoint (so the
    in-VPC Lambdas read secrets and stream CSVs without a NAT)

Writes the created resource ids to infra/network_ids.json for downstream steps.

Run:  uv run python infra/provision_network.py
"""
from __future__ import annotations

import os
import sys

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import PROJECT, REGION, TAGS_MAP, load_ids, save_ids  # noqa: E402

VPC_CIDR = "10.20.0.0/16"
PRIVATE_SUBNETS = [
    {"cidr": "10.20.1.0/24", "az": f"{REGION}a", "name": f"{PROJECT}-private-a"},
    {"cidr": "10.20.2.0/24", "az": f"{REGION}b", "name": f"{PROJECT}-private-b"},
]
VPC_NAME = f"{PROJECT}-vpc"
ROUTE_TABLE_NAME = f"{PROJECT}-private-rt"

ec2 = boto3.client("ec2", region_name=REGION)


def _tag(resource_id: str, name: str) -> None:
    ec2.create_tags(Resources=[resource_id], Tags=[
        {"Key": "Name", "Value": name},
        *[{"Key": k, "Value": v} for k, v in TAGS_MAP.items()],
    ])


def _find_vpc() -> str | None:
    r = ec2.describe_vpcs(Filters=[
        {"Name": "tag:Name", "Values": [VPC_NAME]},
    ])["Vpcs"]
    return r[0]["VpcId"] if r else None


def ensure_vpc() -> str:
    existing = _find_vpc()
    if existing:
        print(f"[skip] VPC {VPC_NAME} exists: {existing}")
        return existing
    vpc_id = ec2.create_vpc(CidrBlock=VPC_CIDR)["Vpc"]["VpcId"]
    ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
    # Private DNS resolution is required for interface-endpoint private DNS.
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    _tag(vpc_id, VPC_NAME)
    print(f"[create] VPC {VPC_NAME} ({VPC_CIDR}): {vpc_id}")
    return vpc_id


def _find_subnet_by_cidr(vpc_id: str, cidr: str) -> str | None:
    r = ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "cidr-block", "Values": [cidr]},
    ])["Subnets"]
    return r[0]["SubnetId"] if r else None


def ensure_private_subnets(vpc_id: str) -> list[str]:
    ids = []
    for s in PRIVATE_SUBNETS:
        existing = _find_subnet_by_cidr(vpc_id, s["cidr"])
        if existing:
            print(f"[skip] subnet {s['name']} exists: {existing}")
            ids.append(existing)
            continue
        resp = ec2.create_subnet(
            VpcId=vpc_id, CidrBlock=s["cidr"], AvailabilityZone=s["az"],
            TagSpecifications=[{"ResourceType": "subnet", "Tags": [
                {"Key": "Name", "Value": s["name"]},
                *[{"Key": k, "Value": v} for k, v in TAGS_MAP.items()]]}],
        )
        sid = resp["Subnet"]["SubnetId"]
        ec2.modify_subnet_attribute(SubnetId=sid, MapPublicIpOnLaunch={"Value": False})
        print(f"[create] subnet {s['name']} ({s['az']}, {s['cidr']}): {sid}")
        ids.append(sid)
    return ids


def ensure_private_route_table(vpc_id: str, subnet_ids: list[str]) -> str:
    rts = ec2.describe_route_tables(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "tag:Name", "Values": [ROUTE_TABLE_NAME]},
    ])["RouteTables"]
    if rts:
        rt_id = rts[0]["RouteTableId"]
        print(f"[skip] private route table exists: {rt_id}")
    else:
        rt_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
        _tag(rt_id, ROUTE_TABLE_NAME)
        print(f"[create] private route table: {rt_id} (no 0.0.0.0/0 route)")
    assoc = ec2.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0].get("Associations", [])
    assoc_subnets = {a.get("SubnetId") for a in assoc}
    for sid in subnet_ids:
        if sid not in assoc_subnets:
            ec2.associate_route_table(RouteTableId=rt_id, SubnetId=sid)
            print(f"  associated {sid} -> {rt_id}")
    return rt_id


def _ensure_sg(vpc_id: str, name: str, desc: str) -> str:
    existing = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "group-name", "Values": [name]},
    ])["SecurityGroups"]
    if existing:
        print(f"[skip] SG {name} exists: {existing[0]['GroupId']}")
        return existing[0]["GroupId"]
    sg_id = ec2.create_security_group(GroupName=name, Description=desc, VpcId=vpc_id)["GroupId"]
    _tag(sg_id, name)
    print(f"[create] SG {name}: {sg_id}")
    return sg_id


def _authorize(sg_id: str, ip_perms: list, label: str) -> None:
    try:
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=ip_perms)
        print(f"  ingress added to {label}")
    except ec2.exceptions.ClientError as e:
        if "InvalidPermission.Duplicate" in str(e):
            print(f"  ingress already present on {label}")
        else:
            raise


def ensure_security_groups(vpc_id: str) -> dict:
    aurora = _ensure_sg(vpc_id, f"{PROJECT}-aurora-sg", "Aurora: 5432 from lambda-sg only")
    lam = _ensure_sg(vpc_id, f"{PROJECT}-lambda-sg", "Tool Lambda ENIs")
    vpce = _ensure_sg(vpc_id, f"{PROJECT}-vpce-sg", "Interface VPC endpoints")

    _authorize(aurora, [{
        "IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
        "UserIdGroupPairs": [{"GroupId": lam, "Description": "psql from tool lambda"}],
    }], "aurora-sg(5432<-lambda-sg)")
    _authorize(vpce, [{
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
        "UserIdGroupPairs": [{"GroupId": lam, "Description": "https from tool lambda"}],
    }], "vpce-sg(443<-lambda-sg)")
    return {"aurora_sg": aurora, "lambda_sg": lam, "vpce_sg": vpce}


def ensure_secrets_endpoint(vpc_id: str, subnet_ids: list[str], vpce_sg: str) -> str:
    eps = ec2.describe_vpc_endpoints(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "service-name", "Values": [f"com.amazonaws.{REGION}.secretsmanager"]},
    ])["VpcEndpoints"]
    if eps:
        print(f"[skip] secretsmanager interface endpoint exists: {eps[0]['VpcEndpointId']}")
        return eps[0]["VpcEndpointId"]
    ep = ec2.create_vpc_endpoint(
        VpcEndpointType="Interface",
        VpcId=vpc_id,
        ServiceName=f"com.amazonaws.{REGION}.secretsmanager",
        SubnetIds=subnet_ids,
        SecurityGroupIds=[vpce_sg],
        PrivateDnsEnabled=True,
        TagSpecifications=[{"ResourceType": "vpc-endpoint", "Tags": [
            {"Key": "Name", "Value": f"{PROJECT}-secrets-endpoint"},
            *[{"Key": k, "Value": v} for k, v in TAGS_MAP.items()]]}],
    )["VpcEndpoint"]
    print(f"[create] secretsmanager interface endpoint: {ep['VpcEndpointId']}")
    return ep["VpcEndpointId"]


def ensure_s3_gateway_endpoint(vpc_id: str, route_table_id: str) -> str:
    service = f"com.amazonaws.{REGION}.s3"
    eps = ec2.describe_vpc_endpoints(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "service-name", "Values": [service]},
    ])["VpcEndpoints"]
    for ep in eps:
        if ep.get("VpcEndpointType") == "Gateway":
            eid = ep["VpcEndpointId"]
            if route_table_id not in ep.get("RouteTableIds", []):
                ec2.modify_vpc_endpoint(VpcEndpointId=eid, AddRouteTableIds=[route_table_id])
                print(f"[update] associated S3 gateway endpoint {eid} with {route_table_id}")
            else:
                print(f"[skip] S3 gateway endpoint {eid} already on {route_table_id}")
            return eid
    created = ec2.create_vpc_endpoint(
        VpcEndpointType="Gateway",
        VpcId=vpc_id,
        ServiceName=service,
        RouteTableIds=[route_table_id],
        TagSpecifications=[{"ResourceType": "vpc-endpoint", "Tags": [
            {"Key": "Name", "Value": f"{PROJECT}-s3-endpoint"},
            *[{"Key": k, "Value": v} for k, v in TAGS_MAP.items()]]}],
    )["VpcEndpoint"]
    print(f"[create] S3 gateway endpoint: {created['VpcEndpointId']}")
    return created["VpcEndpointId"]


def main() -> None:
    print(f"Region {REGION}\n")
    vpc_id = ensure_vpc()
    subnet_ids = ensure_private_subnets(vpc_id)
    rt_id = ensure_private_route_table(vpc_id, subnet_ids)
    sgs = ensure_security_groups(vpc_id)
    secrets_ep = ensure_secrets_endpoint(vpc_id, subnet_ids, sgs["vpce_sg"])
    s3_ep = ensure_s3_gateway_endpoint(vpc_id, rt_id)

    ids = load_ids()
    ids.update({
        "vpc_id": vpc_id,
        "private_subnet_ids": subnet_ids,
        "private_route_table_id": rt_id,
        "secrets_vpc_endpoint_id": secrets_ep,
        "s3_gateway_endpoint_id": s3_ep,
        **sgs,
    })
    save_ids(ids)
    print(f"\nwrote networking ids to network_ids.json:")
    print(f"  vpc_id             = {vpc_id}")
    print(f"  private_subnet_ids = {subnet_ids}")
    print(f"  aurora_sg={sgs['aurora_sg']} lambda_sg={sgs['lambda_sg']} vpce_sg={sgs['vpce_sg']}")
    print(f"  secrets_endpoint={secrets_ep} s3_endpoint={s3_ep}")


if __name__ == "__main__":
    main()
