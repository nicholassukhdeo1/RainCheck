"""Offline semantic checks for the RC-11c SAM deployment and runbook."""

from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "template.yaml"
README_PATH = ROOT / "README.md"
RUNTIME_ARTIFACT_FILES = {
    "db.py",
    "digest.py",
    "ebay_pricing.py",
    "http_retry.py",
    "lambda_handler.py",
    "matching.py",
    "schema.sql",
    "sendemail.py",
    "yahoo_scrape.py",
}


def _template():
    with TEMPLATE_PATH.open(encoding="utf-8") as template_file:
        import yaml

        class CloudFormationLoader(yaml.SafeLoader):
            """Keep CloudFormation intrinsics as data instead of executing YAML tags."""

        def intrinsic(loader, tag_suffix, node):
            if isinstance(node, yaml.ScalarNode):
                value = loader.construct_scalar(node)
            elif isinstance(node, yaml.SequenceNode):
                value = loader.construct_sequence(node)
            else:
                value = loader.construct_mapping(node)
            return {tag_suffix: value}

        CloudFormationLoader.add_multi_constructor("!", intrinsic)
        return yaml.load(template_file, Loader=CloudFormationLoader)


def _resources(template, resource_type):
    return [
        resource
        for resource in template.get("Resources", {}).values()
        if resource.get("Type") == resource_type
    ]


def _walk(value):
    if isinstance(value, str):
        yield value
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _all_strings(value):
    return [node for node in _walk(value) if isinstance(node, str)]


def _runbook():
    return README_PATH.read_text(encoding="utf-8").lower()


def test_rc11c_criterion_17_region_guard_and_deployment_region_documentation():
    template = _template()
    strings = _all_strings(template)
    assert "us-east-2" in strings
    rules = template.get("Rules", {})
    region_assertions = list(_walk(rules))
    assert any(
        isinstance(node, dict)
        and node.get("Assert") is not None
        and "us-east-2" in _all_strings(node)
        for node in region_assertions
    ), "SAM must reject deployment in a selected Region other than us-east-2"

    runbook = _runbook()
    assert "us-east-2" in runbook
    assert any(term in runbook for term in ("reject", "only", "must"))
    assert "region" in runbook


def test_rc11c_criterion_18_database_bucket_is_private_encrypted_and_versioned():
    template = _template()
    buckets = [
        resource
        for resource in template.get("Resources", {}).values()
        if resource.get("Type") == "AWS::S3::Bucket"
    ]
    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket.get("DeletionPolicy") == "Retain"
    assert bucket.get("UpdateReplacePolicy") == "Retain"
    properties = bucket["Properties"]
    public_access = properties["PublicAccessBlockConfiguration"]
    assert all(
        public_access[key] is True
        for key in (
            "BlockPublicAcls",
            "BlockPublicPolicy",
            "IgnorePublicAcls",
            "RestrictPublicBuckets",
        )
    )
    ownership = properties["OwnershipControls"]["Rules"]
    assert any(rule.get("ObjectOwnership") == "BucketOwnerEnforced" for rule in ownership)
    encryption = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert any(
        rule.get("ServerSideEncryptionByDefault", {}).get("SSEAlgorithm") == "AES256"
        for rule in encryption
    )
    assert properties["VersioningConfiguration"]["Status"] == "Enabled"
    lifecycle_rules = properties["LifecycleConfiguration"]["Rules"]
    assert any(
        rule.get("Status") == "Enabled"
        and rule.get("NoncurrentVersionExpiration", {}).get("NoncurrentDays") == 30
        for rule in lifecycle_rules
    )

    bucket_policies = _resources(template, "AWS::S3::BucketPolicy")
    assert bucket_policies
    policy_statements = [
        statement
        for bucket_policy in bucket_policies
        for statement in bucket_policy["Properties"]["PolicyDocument"].get("Statement", [])
    ]
    assert any(
        statement.get("Effect") == "Deny"
        and "aws:securetransport" in str(statement.get("Condition", {})).lower()
        and "false" in str(statement.get("Condition", {})).lower()
        for statement in policy_statements
    ), "bucket policy must deny requests that do not use HTTPS"


def test_rc11c_criterion_19_execution_role_has_only_required_resource_scopes():
    template = _template()
    policies = _resources(template, "AWS::IAM::Policy")
    roles = _resources(template, "AWS::IAM::Role")
    functions = _resources(template, "AWS::Serverless::Function")
    assert functions
    policy_documents = [
        policy["Properties"]["PolicyDocument"]
        for policy in policies
        if "PolicyDocument" in policy.get("Properties", {})
    ]
    policy_documents.extend(
        role["Properties"].get("Policies", [])[index]["PolicyDocument"]
        for role in roles
        for index in range(len(role["Properties"].get("Policies", [])))
    )
    policy_documents.extend(
        policy["PolicyDocument"]
        for function in functions
        for policy in function.get("Properties", {}).get("Policies", [])
        if isinstance(policy, dict) and "PolicyDocument" in policy
    )
    statements = [
        statement
        for document in policy_documents
        for statement in document.get("Statement", [])
    ]
    actions = {
        action
        for statement in statements
        for action in (
            statement["Action"]
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action")]
        )
    }
    assert "s3:GetObject" in actions
    assert "s3:PutObject" in actions
    assert "ssm:GetParameter" in actions
    assert "logs:CreateLogStream" in actions
    assert "logs:PutLogEvents" in actions
    assert "s3:ListBucket" not in actions
    assert "ssm:GetParametersByPath" not in actions
    assert "ssm:GetParameters" not in actions

    s3_statements = [
        statement
        for statement in statements
        if any(
            action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
            for action in ("s3:GetObject", "s3:PutObject")
        )
    ]
    assert s3_statements
    assert all("/*" not in str(statement.get("Resource")) for statement in s3_statements)
    assert all("*" not in str(statement.get("Resource")) for statement in s3_statements)
    assert all("/" in str(statement.get("Resource")) for statement in s3_statements)

    ssm_statements = [statement for statement in statements if "ssm:GetParameter" in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])]
    assert ssm_statements
    ssm_scope = " ".join(str(statement.get("Resource")) for statement in ssm_statements).lower()
    assert all(secret_name in ssm_scope for secret_name in ("ebay", "sender"))
    assert "*" not in ssm_scope

    log_statements = [
        statement
        for statement in statements
        if any(
            action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
            for action in ("logs:CreateLogStream", "logs:PutLogEvents")
        )
    ]
    assert log_statements
    for statement in log_statements:
        resource_strings = _all_strings(statement.get("Resource"))
        assert resource_strings
        assert all(
            "log-group:" in resource and resource.endswith(":*")
            for resource in resource_strings
        )


def test_rc11c_criterion_20_embedded_schedule_is_disabled_local_time_bounded_and_not_retried_by_lambda():
    template = _template()
    functions = _resources(template, "AWS::Serverless::Function")
    schedule_events = [
        event.get("Properties", {})
        for function in functions
        for event in function.get("Properties", {}).get("Events", {}).values()
        if event.get("Type") == "ScheduleV2"
    ]
    assert schedule_events
    schedule = schedule_events[0]
    assert schedule["State"] == "DISABLED"
    assert schedule["ScheduleExpression"] == "cron(0 9 * * ? *)"
    assert schedule["ScheduleExpressionTimezone"] == "Asia/Tokyo"
    assert schedule["FlexibleTimeWindow"]["Mode"] == "OFF"
    retry = schedule["RetryPolicy"]
    assert isinstance(retry.get("MaximumRetryAttempts"), int)
    assert 0 <= retry["MaximumRetryAttempts"] <= 185
    assert isinstance(retry.get("MaximumEventAgeInSeconds"), int)
    assert 60 <= retry["MaximumEventAgeInSeconds"] <= 86400

    assert any(
        function.get("Properties", {}).get("EventInvokeConfig", {}).get("MaximumRetryAttempts") == 0
        for function in functions
    )


def test_rc11c_criterion_21_single_reserved_execution_without_vpc_or_public_api():
    template = _template()
    functions = _resources(template, "AWS::Serverless::Function")
    assert functions
    assert all(
        function["Properties"]["ReservedConcurrentExecutions"] == 1
        for function in functions
    )
    assert all("VpcConfig" not in function["Properties"] for function in functions)
    assert all("FunctionUrlConfig" not in function["Properties"] for function in functions)
    assert all(
        event.get("Type") == "ScheduleV2"
        for function in functions
        for event in function.get("Properties", {}).get("Events", {}).values()
    )
    forbidden_types = {
        "AWS::Lambda::Url",
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::Serverless::Api",
        "AWS::Serverless::HttpApi",
    }
    assert not any(resource.get("Type") in forbidden_types for resource in template.get("Resources", {}).values())


def test_rc11c_criterion_22_package_is_pinned_and_excludes_local_or_sensitive_files(tmp_path):
    template = _template()
    functions = _resources(template, "AWS::Serverless::Function")
    assert functions
    assert all(function["Properties"].get("CodeUri") == "." for function in functions)
    function_name, function = next(
        (name, resource)
        for name, resource in template.get("Resources", {}).items()
        if resource.get("Type") == "AWS::Serverless::Function"
    )
    assert function.get("Metadata", {}).get("BuildMethod") == "makefile"
    requirements = ROOT / "requirements.txt"
    pinned_requirements = [
        line.strip()
        for line in requirements.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert pinned_requirements
    assert all("==" in requirement for requirement in pinned_requirements)
    assert any(requirement.lower().startswith("boto3==") for requirement in pinned_requirements)

    artifact_path = tmp_path / "sam-source-only"
    result = subprocess.run(
        [
            "make",
            f"build-{function_name}",
            f"ARTIFACTS_DIR={artifact_path}",
            "PIP=true",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    artifact_files = {
        str(path.relative_to(artifact_path))
        for path in artifact_path.rglob("*")
        if path.is_file()
    }
    assert artifact_files == RUNTIME_ARTIFACT_FILES
    assert not any(
        "test" in path.lower()
        or "fixture" in path.lower()
        or ".env" in path.lower()
        or ".db" in path.lower()
        or "secret" in path.lower()
        for path in artifact_files
    )

    dependency_artifact = tmp_path / "sam-with-dependency"
    fake_pip = tmp_path / "fake-pip"
    fake_pip_marker = tmp_path / "fake-pip-ran"
    fake_pip.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"artifact = Path({str(dependency_artifact)!r})\n"
        f"Path({str(fake_pip_marker)!r}).write_text('ran')\n"
        "dependency_tests = artifact / 'certifi' / 'tests'\n"
        "dependency_tests.mkdir(parents=True, exist_ok=True)\n"
        "(dependency_tests / 'test_certify.py').write_text('# synthetic dependency test')\n",
        encoding="utf-8",
    )
    fake_pip.chmod(0o755)
    result = subprocess.run(
        [
            "make",
            f"build-{function_name}",
            f"ARTIFACTS_DIR={dependency_artifact}",
            f"PIP={fake_pip}",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert fake_pip_marker.read_text(encoding="utf-8") == "ran"
    installed_files = {
        str(path.relative_to(dependency_artifact))
        for path in dependency_artifact.rglob("*")
        if path.is_file()
    }
    assert not any(
        "/tests/" in f"/{path}" or Path(path).name.startswith("test_")
        for path in installed_files
    ), "Lambda artifact must exclude dependency test files"


def test_rc11c_sam_build_output_directory_is_ignored_by_git():
    result = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "--quiet",
            ".aws-sam/build.toml",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, ".aws-sam/ build artifacts must be ignored by Git"


def test_rc11c_criterion_23_preflight_requires_gate_parameters_backup_integrity_and_s3_version():
    runbook = _runbook()
    for required in (
        "cloudshell",
        "us-east-2",
        "brand",
        "parameter",
        "backup",
        "integrity_check",
        "foreign_key_check",
        "version",
        "schedule",
    ):
        assert required in runbook
    assert "value" in runbook and any(term in runbook for term in ("without", "never", "do not"))


def test_rc11c_criterion_23_and_24_deploy_commands_set_artifact_bucket_and_isolate_dry_run():
    runbook = _runbook()
    production_section = runbook.split("### production preflight", 1)[1].split(
        "### isolated dry-run", 1
    )[0]
    production_deploy_commands = [
        line for line in production_section.splitlines()
        if "sam deploy" in line and "--no-execute-changeset" in line
    ]
    assert production_deploy_commands, "production change-set command must be documented"
    assert any(
        "--resolve-s3" in command or "--s3-bucket" in command
        for command in production_deploy_commands
    ), "SAM must have an artifact bucket option before it can package the zip"

    dry_run_section = runbook.split("### isolated dry-run", 1)[1].split("### rollback", 1)[0]
    dry_run_commands = [line for line in dry_run_section.splitlines() if "sam deploy" in line]
    assert any(
        "--stack-name" in command
        and "--parameter-overrides" in command
        and "dryrun=1" in command
        and ("--resolve-s3" in command or "--s3-bucket" in command)
        for command in dry_run_commands
    ), "dry-run deployment must target a separate stack and configure its SAM artifact bucket"


def test_rc11c_criterion_23_runbook_uploads_and_verifies_exact_s3_object_version():
    runbook = _runbook()
    production_section = runbook.split("### production preflight", 1)[1].split(
        "### isolated dry-run", 1
    )[0]
    continued_lines = re.sub(r"\\\n\s*", " ", production_section)
    put_commands = [
        line for line in continued_lines.splitlines()
        if "aws s3api put-object" in line
    ]
    assert put_commands, "runbook must show the production S3 upload command"
    put_command = " ".join(put_commands)
    assert re.search(r"--server-side-encryption\s+aes256\b", put_command)
    assert re.search(r"--if-none-match\s+(?:'\*'|\"\*\")", put_command), (
        "initial upload must fail if the database object already exists"
    )
    assert "version_id" in put_command
    assert re.search(r"--query\s+versionid\b", put_command)
    assert re.search(r"--output\s+text\b", put_command)

    get_commands = [
        line for line in continued_lines.splitlines()
        if "aws s3api get-object" in line
    ]
    assert get_commands, "runbook must show version-specific S3 download verification"
    get_command = " ".join(get_commands)
    assert "--version-id" in get_command
    assert "version_id" in get_command


def test_rc11c_criterion_23_runbook_checks_foreign_key_sqlite_exit_status():
    runbook = _runbook()
    production_section = runbook.split("### production preflight", 1)[1].split(
        "### isolated dry-run", 1
    )[0]
    continued_lines = re.sub(r"\\\n\s*", " ", production_section)
    assert "foreign_key_check" in continued_lines
    assert re.search(
        r"if\s+!\s+sqlite3\b[^\n]*foreign_key_check"
        r"|sqlite3\b[^\n]*foreign_key_check[^\n]*\|\|",
        continued_lines,
    ), "SQLite foreign-key check must fail on sqlite3 errors separately from its empty-result check"


def test_rc11c_criterion_24_dry_run_is_isolated_synthetic_and_cannot_write_production_state():
    runbook = _runbook()
    for required in (
        "dry-run",
        "synthetic",
        "throwaway",
        "raincheck_dry_run=1",
        "production",
        "schedule",
        "email",
    ):
        assert required in runbook
    assert any(term in runbook for term in ("separate bucket", "separate database", "separate s3"))
    assert any(term in runbook for term in ("enable", "disabled"))


def test_rc11c_criterion_25_offline_verification_and_rollback_are_documented():
    runbook = _runbook()
    assert "pytest" in runbook
    assert "no real aws" in runbook or "without aws" in runbook
    assert "rollback" in runbook
    assert "disable" in runbook and "schedule" in runbook
    assert "prior" in runbook and "version" in runbook


def _rc12_resources(template, resource_type):
    return _resources(template, resource_type)


def _rc12_parameter(template, name):
    return template.get("Parameters", {}).get(name, {})


def _rc12_named_resources(template, resource_type):
    return {
        name: resource
        for name, resource in template.get("Resources", {}).items()
        if resource.get("Type") == resource_type
    }


def test_rc12_criterion_1_monthly_account_budget_uses_required_positive_usd_parameter():
    template = _template()
    budgets = _rc12_resources(template, "AWS::Budgets::Budget")
    assert len(budgets) == 1
    budget = budgets[0]["Properties"]["Budget"]
    assert budget["BudgetType"] == "COST"
    assert budget["TimeUnit"] == "MONTHLY"
    assert budget["BudgetLimit"]["Unit"] == "USD"
    assert budget["BudgetLimit"]["Amount"] == {"Ref": "MonthlyBudgetAmountUsd"}

    amount = _rc12_parameter(template, "MonthlyBudgetAmountUsd")
    assert amount.get("Type") == "Number"
    assert "Default" not in amount
    assert float(amount.get("MinValue", 0)) > 0
    assert "monthly" in amount.get("Description", "").lower()
    assert "usd" in amount.get("Description", "").lower()


def test_rc12_criterion_2_budget_sends_one_actual_cost_80_percent_email_without_actions():
    template = _template()
    budgets = _rc12_resources(template, "AWS::Budgets::Budget")
    assert len(budgets) == 1
    properties = budgets[0]["Properties"]
    notifications = properties["NotificationsWithSubscribers"]
    assert len(notifications) == 1
    notification = notifications[0]["Notification"]
    assert notification["NotificationType"] == "ACTUAL"
    assert notification["ComparisonOperator"] == "GREATER_THAN"
    assert notification["ThresholdType"] == "PERCENTAGE"
    assert notification["Threshold"] == 80
    subscribers = notifications[0]["Subscribers"]
    assert subscribers == [
        {"SubscriptionType": "EMAIL", "Address": {"Ref": "AlertEmail"}}
    ]
    assert "BudgetActions" not in properties


def test_rc12_criterion_3_budget_is_account_wide_and_runbook_explains_unrelated_charges():
    template = _template()
    budgets = _rc12_resources(template, "AWS::Budgets::Budget")
    assert len(budgets) == 1
    budget = budgets[0]["Properties"]["Budget"]
    assert not budget.get("CostFilters"), "account-wide budget must not restrict cost filters"
    runbook = _runbook()
    assert "account-wide" in runbook or "underlying aws account" in runbook
    assert "unrelated" in runbook and "charges" in runbook


def test_rc12_criterion_4_alert_email_is_secret_parameter_for_single_sns_email_subscription():
    template = _template()
    email = _rc12_parameter(template, "AlertEmail")
    assert email.get("NoEcho") is True
    assert "Default" not in email
    assert email.get("Type") == "String"
    pattern = email.get("AllowedPattern", "")
    assert "@" in pattern and "." in pattern
    assert int(email.get("MinLength", 0)) > 0 or pattern

    topics = _rc12_named_resources(template, "AWS::SNS::Topic")
    subscriptions = _rc12_resources(template, "AWS::SNS::Subscription")
    assert len(topics) == 1
    assert len(subscriptions) == 1
    subscription = subscriptions[0]["Properties"]
    assert subscription["Protocol"] == "email"
    assert subscription["Endpoint"] == {"Ref": "AlertEmail"}
    assert subscription["TopicArn"] == {"Ref": next(iter(topics))}

    resource_names = list(template.get("Resources", {}))
    outputs = template.get("Outputs", {})
    output_values = _all_strings(outputs)
    assert all("@" not in name for name in resource_names)
    assert not any("@" in value for value in output_values)
    assert not any(
        node == {"Ref": "AlertEmail"}
        for node in _walk(outputs)
        if isinstance(node, dict)
    )


def test_rc12_criterion_5_sns_publish_policy_is_limited_to_alarm_and_account():
    template = _template()
    policies = _rc12_resources(template, "AWS::SNS::TopicPolicy")
    assert len(policies) == 1
    policy = policies[0]["Properties"]
    topics = _rc12_named_resources(template, "AWS::SNS::Topic")
    assert policy["Topics"] == [{"Ref": next(iter(topics))}]
    statements = policy["PolicyDocument"].get("Statement", [])
    publish_statements = [
        statement
        for statement in statements
        if "sns:Publish" in (
            statement["Action"]
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action")]
        )
    ]
    assert len(publish_statements) == 1
    statement = publish_statements[0]
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    condition = statement["Condition"]
    alarms = _rc12_named_resources(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) == 1
    source_arn = condition.get("ArnEquals", {}).get("aws:SourceArn")
    assert source_arn == {"Fn::GetAtt": [next(iter(alarms)), "Arn"]}
    assert "*" not in str(source_arn)
    assert condition.get("StringEquals", {}).get("aws:SourceAccount") == {"Ref": "AWS::AccountId"}
    assert statement.get("Resource") == {"Ref": next(iter(topics))}
    assert not any(
        resource.get("Type") == "AWS::IAM::Policy"
        and "sns:Publish" in str(resource.get("Properties", {}).get("PolicyDocument", {}))
        for resource in template.get("Resources", {}).values()
    )
    for function in _rc12_resources(template, "AWS::Serverless::Function"):
        assert "sns:Publish" not in str(function.get("Properties", {}).get("Policies", []))


def test_rc12_criterion_6_alarm_counts_function_errors_over_one_five_minute_period():
    template = _template()
    alarms = _rc12_named_resources(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) == 1
    alarm = next(iter(alarms.values()))["Properties"]
    assert alarm["Namespace"] == "AWS/Lambda"
    assert alarm["MetricName"] == "Errors"
    assert alarm["Dimensions"] == [
        {"Name": "FunctionName", "Value": {"Ref": "RainCheckFunction"}}
    ]
    assert alarm["Statistic"] == "Sum"
    assert alarm["Period"] == 300
    assert alarm["EvaluationPeriods"] == 1
    assert alarm["DatapointsToAlarm"] == 1
    assert alarm["Threshold"] == 0
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    assert alarm["TreatMissingData"] == "notBreaching"


def test_rc12_criterion_7_alarm_only_notifies_topic_on_alarm_transition():
    template = _template()
    alarms = _rc12_resources(template, "AWS::CloudWatch::Alarm")
    topics = _rc12_named_resources(template, "AWS::SNS::Topic")
    assert len(alarms) == 1
    properties = alarms[0]["Properties"]
    assert properties["AlarmActions"] == [{"Ref": next(iter(topics))}]
    assert properties.get("ActionsEnabled") is True
    assert not properties.get("OKActions")
    assert not properties.get("InsufficientDataActions")


def test_rc12_criterion_8_region_guard_and_disabled_schedule_remain_with_budget_preflight():
    template = _template()
    rules = template.get("Rules", {})
    assert "SelectedRegion" in rules
    assert any("us-east-2" in _all_strings(assertion) for assertion in rules["SelectedRegion"]["Assertions"])
    functions = _rc12_resources(template, "AWS::Serverless::Function")
    schedules = [
        event["Properties"]
        for function in functions
        for event in function.get("Properties", {}).get("Events", {}).values()
        if event.get("Type") == "ScheduleV2"
    ]
    assert len(schedules) == 1 and schedules[0]["State"] == "DISABLED"

    runbook = _runbook()
    assert "us-east-2" in runbook
    assert "aws::budgets::budget" in runbook
    assert "resource specification" in runbook
    assert "stop" in runbook and "absent" in runbook


def test_rc12_criterion_9_runbook_requires_inputs_change_set_review_and_private_status_checks():
    runbook = _runbook()
    for required in (
        "monthlybudgetamountusd",
        "alertemail",
        "change set",
        "review",
        "approve",
        "budget",
        "alarm",
        "topic policy",
        "subscription",
    ):
        assert required in runbook
    assert any(term in runbook for term in ("noecho", "masked", "redact"))
    assert any(term in runbook for term in ("shared logs", "logs", "credentials"))

    production_section = runbook.split("### production preflight", 1)[1].split(
        "### isolated dry-run", 1
    )[0]
    sam_command = next(
        line
        for line in production_section.splitlines()
        if "sam deploy" in line and "--resolve-s3" in line and "--no-execute-changeset" in line
    )
    command_position = production_section.index(sam_command)
    before_artifact_preparation = production_section[:command_position]
    assert "authorize" in before_artifact_preparation or "authorization" in before_artifact_preparation
    assert "artifact" in before_artifact_preparation and "bucket" in before_artifact_preparation

    after_command = production_section[command_position:]
    review_position = after_command.index("review")
    approval_position = after_command.index("approv", review_position)
    execution_position = after_command.index("execut", approval_position)
    assert review_position < approval_position < execution_position

    assert not re.search(r"\bread\s+-[^\n]*\s-p(?:\s|\b)", production_section), (
        "the prompt must not use Bash-only read -p syntax; support the user's zsh shell"
    )


def test_rc12_criterion_15_alert_resources_and_outputs_are_production_only():
    template = _template()
    alert_resource_types = {
        "AWS::Budgets::Budget",
        "AWS::SNS::Topic",
        "AWS::SNS::Subscription",
        "AWS::SNS::TopicPolicy",
        "AWS::CloudWatch::Alarm",
    }
    alert_resources = [
        (name, resource)
        for name, resource in template.get("Resources", {}).items()
        if resource.get("Type") in alert_resource_types
    ]
    assert len(alert_resources) == len(alert_resource_types)

    conditions = template.get("Conditions", {})
    production_conditions = {
        name
        for name, expression in conditions.items()
        if any(
            isinstance(node, dict)
            and any(key in node for key in ("Equals", "Fn::Equals"))
            and len(node.get("Equals", node.get("Fn::Equals", []))) == 2
            and {"Ref": "DryRun"} in node.get("Equals", node.get("Fn::Equals", []))
            and any(
                str(operand) == "0"
                for operand in node.get("Equals", node.get("Fn::Equals", []))
            )
            for node in _walk(expression)
        )
    }
    assert production_conditions, "template must define a condition that is true only when DryRun=0"
    assert all(resource.get("Condition") in production_conditions for _, resource in alert_resources)

    alert_output_names = {"AlertTopicArn", "ErrorAlarmName"}
    outputs = template.get("Outputs", {})
    assert alert_output_names.issubset(outputs)
    assert all(
        outputs[name].get("Condition") in production_conditions
        for name in alert_output_names
    )


def test_rc12_criterion_10_runbook_verifies_budget_email_and_confirms_sns_subscription_separately():
    runbook = _runbook()
    assert "budgets" in runbook and "email" in runbook and "verify" in runbook
    assert "sns" in runbook and "subscription" in runbook and "confirm" in runbook
    assert any(term in runbook for term in ("subscriptionconfirmed", "subscription status", "confirmed"))
    assert any(term in runbook for term in ("budget notification", "notification status", "subscriber"))


def test_rc12_criterion_11_runbook_tests_alarm_email_without_invoking_production_or_changing_database():
    runbook = _runbook()
    assert "setalarmstate" in runbook
    assert "alarm history" in runbook
    assert "alarm email" in runbook or "email" in runbook
    assert "without invoking" in runbook or "does not invoke" in runbook
    assert "production database" in runbook
    assert "budget" in runbook and any(term in runbook for term in ("without artificially creating charges", "without creating charges"))


def test_rc12_criterion_12_runbook_lists_cost_categories_and_says_alerts_do_not_stop_charges():
    runbook = _runbook()
    for category in (
        "lambda",
        "s3",
        "ssm",
        "eventbridge scheduler",
        "cloudwatch",
        "sns",
        "budgets",
    ):
        assert category in runbook
    assert "pricing" in runbook
    assert "existing budget" in runbook or "budget count" in runbook
    assert "do not stop charges" in runbook or "does not stop charges" in runbook


def test_rc12_criterion_13_runbook_distinguishes_absent_invocation_and_delayed_billing():
    runbook = _runbook()
    assert "missing invocation" in runbook or "no invocation" in runbook
    assert "disabled schedule" in runbook or "schedule is disabled" in runbook
    assert "undelivered" in runbook and "scheduler" in runbook
    assert "errors alarm" in runbook
    assert "delayed" in runbook and "billing" in runbook
    assert "real-time" in runbook or "real time" in runbook
