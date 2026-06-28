from prometheus_client import Counter, Gauge, Histogram

VPCS_CREATED = Counter(
    "vpcs_created_total",
    "Number of VPCs created"
)

VPCS_DELETED = Counter(
    "vpcs_deleted_total",
    "Number of VPCs deleted"
)

LOGIN_SUCCESS = Counter(
    "login_success_total",
    "Successful logins"
)

LOGIN_FAILED = Counter(
    "login_failed_total",
    "Failed logins"
)

AWS_ERRORS = Counter(
    "aws_errors_total",
    "AWS API errors"
)

CURRENT_VPCS = Gauge(
    "current_vpcs",
    "Current VPC count"
)

REQUEST_TIME = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration"
)