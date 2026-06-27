import hmac
import ipaddress
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, List

import bcrypt
import boto3
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, Field, field_validator, model_validator

import database

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(title="VPC Creator API", version="1.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_ADMIN_HASH = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt())
_bearer = HTTPBearer()


def create_token(sub: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": sub, "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        username: str | None = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SubnetConfig(BaseModel):
    cidr: str
    availability_zone: str

    @field_validator("cidr")
    @classmethod
    def validate_cidr(cls, v: str) -> str:
        try:
            net = ipaddress.ip_network(v, strict=True)
        except ValueError as exc:
            raise ValueError(f"Invalid subnet CIDR: {exc}") from exc
        if not (16 <= net.prefixlen <= 28):
            raise ValueError("Subnet prefix must be /16–/28")
        return v

    @field_validator("availability_zone")
    @classmethod
    def validate_az(cls, v: str) -> str:
        if not re.match(r"^[a-z]{2}-[a-z]+-\d[a-z]$", v):
            raise ValueError(f"Invalid AZ format '{v}' — expected e.g. us-east-1a")
        return v


class VPCCreate(BaseModel):
    cidr: str
    region: str
    subnets: List[SubnetConfig] = Field(..., min_length=1)

    @field_validator("cidr")
    @classmethod
    def validate_cidr(cls, v: str) -> str:
        try:
            ipaddress.ip_network(v, strict=True)
        except ValueError as exc:
            raise ValueError(f"Invalid VPC CIDR: {exc}") from exc
        return v

    @field_validator("region")
    @classmethod
    def validate_region(cls, v: str) -> str:
        if not re.match(r"^[a-z]{2}-[a-z]+-\d$", v):
            raise ValueError(f"Invalid region '{v}' — expected e.g. us-east-1")
        return v

    @model_validator(mode="after")
    def validate_subnets(self) -> "VPCCreate":
        vpc_net = ipaddress.ip_network(self.cidr, strict=True)
        seen: list[ipaddress.IPv4Network] = []
        for s in self.subnets:
            sub_net = ipaddress.ip_network(s.cidr, strict=True)
            if not sub_net.subnet_of(vpc_net):
                raise ValueError(f"Subnet {s.cidr} is not within VPC CIDR {self.cidr}")
            for existing in seen:
                if sub_net.overlaps(existing):
                    raise ValueError(f"Subnet {s.cidr} overlaps with {existing}")
            seen.append(sub_net)
        for s in self.subnets:
            if not s.availability_zone.startswith(self.region):
                raise ValueError(
                    f"AZ '{s.availability_zone}' does not belong to region '{self.region}'"
                )
        return self


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------


def _build_vpc(body: VPCCreate) -> dict:
    ec2 = boto3.Session().client("ec2", region_name=body.region)
    vpc_id: str | None = None
    igw_id: str | None = None

    vpc_id = ec2.create_vpc(CidrBlock=body.cidr)["Vpc"]["VpcId"]
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})

    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    rt_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    ec2.create_route(
        RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id
    )

    subnets = []
    for s in body.subnets:
        sub_id = ec2.create_subnet(
            VpcId=vpc_id, CidrBlock=s.cidr, AvailabilityZone=s.availability_zone
        )["Subnet"]["SubnetId"]
        ec2.associate_route_table(RouteTableId=rt_id, SubnetId=sub_id)
        subnets.append(
            {"subnet_id": sub_id, "cidr": s.cidr, "availability_zone": s.availability_zone}
        )

    ec2.create_tags(
        Resources=[vpc_id, igw_id, rt_id],
        Tags=[{"Key": "CreatedBy", "Value": "vpc-creator-api"}],
    )

    return {
        "id": str(uuid.uuid4()),
        "aws_vpc_id": vpc_id,
        "cidr": body.cidr,
        "region": body.region,
        "subnets": subnets,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _cleanup(ec2, vpc_id: str | None, igw_id: str | None) -> None:
    try:
        if igw_id and vpc_id:
            ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw_id)
        if vpc_id:
            ec2.delete_vpc(VpcId=vpc_id)
    except Exception:
        pass


def _teardown_vpc(ec2, aws_vpc_id: str, subnets: list) -> None:
    # 1. Detach and delete all Internet Gateways attached to this VPC.
    igws = ec2.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [aws_vpc_id]}]
    )["InternetGateways"]
    for igw in igws:
        igw_id = igw["InternetGatewayId"]
        ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=aws_vpc_id)
        ec2.delete_internet_gateway(InternetGatewayId=igw_id)

    # 2 + 3. Disassociate and delete custom route tables.
    rts = ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [aws_vpc_id]}]
    )["RouteTables"]
    for rt in rts:
        is_main = any(a.get("Main") for a in rt.get("Associations", []))
        if is_main:
            continue
        for assoc in rt.get("Associations", []):
            if assoc.get("RouteTableAssociationId"):
                ec2.disassociate_route_table(
                    AssociationId=assoc["RouteTableAssociationId"]
                )
        ec2.delete_route_table(RouteTableId=rt["RouteTableId"])

    # 4. Delete subnets (we stored them at creation time).
    for subnet in subnets:
        ec2.delete_subnet(SubnetId=subnet["subnet_id"])

    # 5. Delete the VPC — all dependencies are now gone.
    ec2.delete_vpc(VpcId=aws_vpc_id)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
User = Annotated[str, Depends(get_current_user)]


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/login", response_model=TokenResponse, tags=["auth"])
def login(body: LoginRequest) -> TokenResponse:
    valid = hmac.compare_digest(body.username, ADMIN_USERNAME) and bcrypt.checkpw(
        body.password.encode(), _ADMIN_HASH
    )
    if not valid:
        logger.warning("Failed login attempt: %s", body.username)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    logger.info("Login successful: %s", body.username)
    return TokenResponse(access_token=create_token(body.username))


@app.post("/vpcs", status_code=201, tags=["vpcs"])
def create_vpc(body: VPCCreate, user: User) -> dict:
    ec2 = boto3.Session().client("ec2", region_name=body.region)
    vpc_id = igw_id = None
    try:
        vpc = _build_vpc(body)
    except ClientError as exc:
        logger.error("AWS error creating VPC: %s", exc)
        _cleanup(ec2, vpc_id, igw_id)
        raise HTTPException(
            status_code=400, detail=exc.response["Error"]["Message"]
        )
    database.save_vpc(vpc)
    logger.info("VPC %s created by %s", vpc["aws_vpc_id"], user)
    return vpc


@app.get("/vpcs", tags=["vpcs"])
def list_vpcs(user: User) -> list:
    return database.list_vpcs()


@app.get("/vpcs/{vpc_id}", tags=["vpcs"])
def get_vpc(vpc_id: str, user: User) -> dict:
    vpc = database.get_vpc(vpc_id)
    if vpc is None:
        raise HTTPException(status_code=404, detail=f"VPC '{vpc_id}' not found")
    return vpc

@app.delete("/vpcs/{vpc_id}", status_code=204, tags=["vpcs"])
def delete_vpc(vpc_id: str, user: User) -> None:
    vpc = database.get_vpc(vpc_id)
    if vpc is None:
        raise HTTPException(status_code=404, detail=f"VPC '{vpc_id}' not found")

    ec2 = boto3.Session().client("ec2", region_name=vpc["region"])
    try:
        _teardown_vpc(ec2, vpc["aws_vpc_id"], vpc["subnets"])
    except ClientError as exc:
        logger.error("AWS error deleting VPC %s: %s", vpc["aws_vpc_id"], exc)
        raise HTTPException(
            status_code=400, detail=exc.response["Error"]["Message"]
        )

    # Only remove from DB after AWS deletion succeeds.
    database.delete_vpc(vpc_id)
    logger.info("VPC %s deleted by %s", vpc["aws_vpc_id"], user)