#final code works with slack both ecs and pipeline logs works
import json
import boto3
import urllib3
from datetime import datetime

codepipeline = boto3.client("codepipeline")
codebuild = boto3.client("codebuild")
ecs = boto3.client("ecs")
logs = boto3.client("logs")

http = urllib3.PoolManager()

SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/"


def lambda_handler(event, context):
    print("EVENT:", json.dumps(event))

    detail = event["detail"]
    
    # Check if this is CodePipeline or ECS event
    if "pipeline" in detail:
        # CodePipeline event
        return handle_codepipeline(detail)
    elif "taskArn" in detail:
        # ECS task event
        return handle_ecs_task(detail)
    
    return {"statusCode": 400, "body": "Unknown event type"}


# ==================================================
# CODEPIPELINE HANDLER
# ==================================================
def handle_codepipeline(detail):
    pipeline_name = detail["pipeline"]
    execution_id = detail["execution-id"]
    state = detail["state"]

    # STEP 1 — find failed stage & action
    failed_stage, failed_action, build_id = extract_failed_action(pipeline_name)
    
    print(f"Failed Stage: {failed_stage}, Failed Action: {failed_action}, Build ID: {build_id}")

    # STEP 2 — fetch logs automatically using build-id
    build_logs = get_codebuild_logs(build_id) if build_id else "No build-id found. Could not fetch logs."

    # STEP 3 — send to Slack
    send_to_slack(
        pipeline_name=pipeline_name,
        execution_id=execution_id,
        failed_stage=failed_stage,
        failed_action=failed_action,
        build_logs=build_logs,
        state=state,
    )

    return {"statusCode": 200, "body": "OK"}


# ==================================================
# ECS TASK HANDLER
# ==================================================
def handle_ecs_task(detail):
    task_arn = detail.get("taskArn")
    last_status = detail.get("lastStatus")
    
    print(f"Task ARN: {task_arn}, Status: {last_status}")
    
    # Extract cluster from task ARN
    # Task ARN format: arn:aws:ecs:region:account:task/cluster-name/task-id
    cluster = extract_cluster_from_task_arn(task_arn)
    
    print(f"Extracted Cluster: {cluster}")
    
    # Only send notifications for failed tasks
    if last_status not in ["STOPPED", "STOPPING"]:
        return {"statusCode": 200, "body": "Task not in failure state"}
    
    # STEP 1 — get task details
    task_name, container_name, service_name = get_ecs_task_details(cluster, task_arn)
    
    print(f"Task Name: {task_name}, Container: {container_name}, Service: {service_name}")

    # STEP 2 — fetch logs
    task_logs = get_ecs_task_logs(cluster, task_arn, container_name) if container_name else "No container found."

    # STEP 3 — send to Slack
    send_to_slack_ecs(
        cluster=cluster,
        task_arn=task_arn,
        task_name=task_name,
        container_name=container_name,
        task_logs=task_logs,
        status=last_status,
    )

    return {"statusCode": 200, "body": "OK"}


def extract_cluster_from_task_arn(task_arn):
    """Extract cluster name from task ARN"""
    # Format: arn:aws:ecs:region:account:task/cluster-name/task-id
    try:
        parts = task_arn.split("/")
        if len(parts) >= 2:
            return parts[-2]
    except:
        pass
    return "default"
def extract_failed_action(pipeline_name):
    try:
        stage_states = codepipeline.get_pipeline_state(name=pipeline_name)

        for stage in stage_states["stageStates"]:
            for action in stage.get("actionStates", []):
                latest = action.get("latestExecution", {})
                if latest.get("status") == "Failed":

                    # Detect CodeBuild build-id
                    external_id = latest.get("externalExecutionId")
                    
                    print(f"External Execution ID: {external_id}")

                    build_id = None
                    if external_id:
                        # externalExecutionId format: arn:aws:codebuild:region:account-id:build/project-name:build-uuid
                        # Extract everything after "build/" 
                        if "build/" in external_id:
                            build_id = external_id.split("build/")[1]
                        else:
                            build_id = external_id
                        print(f"External ID: {external_id}")
                        print(f"Extracted Build ID: {build_id}")

                    return stage["stageName"], action["actionName"], build_id

        return None, None, None
        
    except Exception as e:
        print(f"Error extracting failed action: {str(e)}")
        return None, None, None


# ------------------------------------------------------
# AUTO-DETECT LOG GROUP + STREAM FROM BUILD-ID
# ------------------------------------------------------
def get_codebuild_logs(build_id):
    try:
        print(f"Fetching logs for build ID: {build_id}")
        
        # Fetch full build info
        build_response = codebuild.batch_get_builds(ids=[build_id])
        
        print(f"Build response: {json.dumps(build_response, indent=2, default=str)}")
        
        if not build_response.get("builds"):
            return f"No build found with ID: {build_id}"
        
        build = build_response["builds"][0]

        # AUTO-DETECTED → always correct
        logs_info = build.get("logs", {})

        log_group = logs_info.get("groupName")
        log_stream = logs_info.get("streamName")

        print(f"Log Group: {log_group}")
        print(f"Log Stream: {log_stream}")

        if not log_group or not log_stream:
            return f"No CloudWatch logs found in build metadata. Logs info: {logs_info}"

        print("Auto-detected log group:", log_group)
        print("Auto-detected log stream:", log_stream)

        # Fetch last 3500 log lines
        response = logs.get_log_events(
            logGroupName=log_group,
            logStreamName=log_stream,
            limit=3500,
            startFromHead=False
        )

        events = response.get("events", [])
        if not events:
            return "No log events found."

        # Get last 3500 lines and remove last 1500 characters
        all_logs = "\n".join(e["message"] for e in events)
        
        if len(all_logs) > 1500:
            all_logs = all_logs[:-1500]

        return all_logs

    except Exception as e:
        print(f"Error fetching CodeBuild logs: {str(e)}")
        return f"Error fetching CodeBuild logs: {str(e)}"


# ------------------------------------------------------
# SEND TO SLACK
# ------------------------------------------------------
def send_to_slack(pipeline_name, execution_id, failed_stage, failed_action, build_logs, state):

    if len(build_logs) > 2000:
        build_logs = build_logs[-2000:]

    # Escape special characters in logs for JSON
    build_logs = build_logs.replace("\\", "\\\\").replace('"', '\\"')

    # Check if pipeline succeeded or failed
    if state == "SUCCEEDED":
        msg = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "✅ CodePipeline Succeeded", "emoji": True}
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Pipeline:*\n{pipeline_name}"},
                        {"type": "mrkdwn", "text": f"*Status:*\n{state}"},
                        {"type": "mrkdwn", "text": f"*Execution ID:*\n{execution_id}"},
                        {"type": "mrkdwn", "text": f"*Time:*\n{datetime.now()}"}
                    ]
                }
            ]
        }
    else:
        msg = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "🚨 CodePipeline Failed", "emoji": True}
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Pipeline:*\n{pipeline_name}"},
                        {"type": "mrkdwn", "text": f"*Status:*\n{state}"},
                        {"type": "mrkdwn", "text": f"*Failed Stage:*\n{failed_stage}"},
                        {"type": "mrkdwn", "text": f"*Failed Action:*\n{failed_action}"},
                        {"type": "mrkdwn", "text": f"*Execution ID:*\n{execution_id}"},
                        {"type": "mrkdwn", "text": f"*Time:*\n{datetime.now()}"}
                    ]
                },
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": "*Build Logs (Last 3500 lines - 1500 chars):*"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```{build_logs}```"}}
            ]
        }

    try:
        msg_body = json.dumps(msg).encode("utf-8")
        print(f"Message size: {len(msg_body)} bytes")
        
        response = http.request(
            "POST",
            SLACK_WEBHOOK_URL,
            body=msg_body,
            headers={"Content-Type": "application/json"}
        )

        print("Slack response status:", response.status)
        print("Slack response body:", response.data.decode("utf-8"))
        
    except Exception as e:
        print(f"Error sending to Slack: {str(e)}")


# ==================================================
# ECS TASK FUNCTIONS
# ==================================================
def get_ecs_task_details(cluster, task_arn):
    try:
        print(f"Fetching task details for: {task_arn}, Cluster: {cluster}")
        
        # Use full ARN for cluster if needed
        if not cluster.startswith("arn:"):
            cluster_identifier = cluster
        else:
            cluster_identifier = cluster
        
        response = ecs.describe_tasks(
            cluster=cluster_identifier,
            tasks=[task_arn],
            include=['TAGS']
        )
        
        if not response.get("tasks"):
            return None, None
        
        task = response["tasks"][0]
        task_name = task_arn.split("/")[-1]
        container_name = None
        service_name = None
        
        if task.get("containers"):
            container_name = task["containers"][0].get("name")
        
        # Try to get service name from tags
        if task.get("tags"):
            for tag in task["tags"]:
                if tag.get("key") == "aws:ecs:serviceName":
                    service_name = tag.get("value")
                    break
        
        print(f"Task: {task_name}, Container: {container_name}, Service: {service_name}")
        return task_name, container_name, service_name
        
    except Exception as e:
        print(f"Error getting ECS task details: {str(e)}")
        return None, None, None


def get_ecs_task_logs(cluster, task_arn, container_name):
    try:
        print(f"Fetching logs for container: {container_name}")
        
        response = ecs.describe_tasks(
            cluster=cluster,
            tasks=[task_arn],
            include=['TAGS']
        )
        
        if not response.get("tasks"):
            return "Task not found"
        
        task = response["tasks"][0]
        log_group = None
        log_stream = None
        
        # Get service name from task tags or ARN
        service_name = None
        if task.get("tags"):
            for tag in task["tags"]:
                if tag.get("key") == "aws:ecs:serviceName":
                    service_name = tag.get("value")
                    break
        
        # If not in tags, try to extract from task ARN or use container name
        if not service_name:
            service_name = container_name
        
        print(f"Service Name: {service_name}")
        
        if task.get("containers"):
            for container in task["containers"]:
                if container.get("name") == container_name:
                    # Get log configuration from container
                    log_config = container.get("logDriver")
                    print(f"Log driver: {log_config}")
                    
                    # Try common log group patterns
                    log_groups = [
                        f"/ecs/{service_name}",
                        f"/ecs/{container_name}",
                        f"ecs-logs-{service_name}",
                        f"/aws/ecs/{service_name}",
                    ]
                    
                    for lg in log_groups:
                        try:
                            print(f"Trying log group: {lg}")
                            streams = logs.describe_log_streams(
                                logGroupName=lg,
                                orderBy='LastEventTime',
                                descending=True,
                                limit=1
                            )
                            
                            if streams.get("logStreams"):
                                log_group = lg
                                log_stream = streams["logStreams"][0]["logStreamName"]
                                print(f"Found log stream: {log_stream}")
                                break
                        except Exception as e:
                            print(f"Log group {lg} not found: {str(e)}")
                            continue
        
        if not log_group or not log_stream:
            return "No CloudWatch logs found for this task"
        
        print(f"Log group: {log_group}, Log stream: {log_stream}")
        
        # Fetch last 2000 log lines
        response = logs.get_log_events(
            logGroupName=log_group,
            logStreamName=log_stream,
            limit=2000,
            startFromHead=False
        )
        
        events = response.get("events", [])
        if not events:
            return "No log events found."
        
        all_logs = "\n".join(e["message"] for e in events)
        return all_logs
        
    except Exception as e:
        print(f"Error fetching ECS logs: {str(e)}")
        return f"Error fetching logs: {str(e)}"


def send_to_slack_ecs(cluster, task_arn, task_name, container_name, task_logs, status):
    
    if len(task_logs) > 2000:
        task_logs = task_logs[-2000:]

    task_logs = task_logs.replace("\\", "\\\\").replace('"', '\\"')

    msg = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🚨 ECS Task Failed", "emoji": True}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Cluster:*\n{cluster}"},
                    {"type": "mrkdwn", "text": f"*Status:*\n{status}"},
                    {"type": "mrkdwn", "text": f"*Task Name:*\n{task_name}"},
                    {"type": "mrkdwn", "text": f"*Container:*\n{container_name}"},
                    {"type": "mrkdwn", "text": f"*Time:*\n{datetime.now()}"}
                ]
            },
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Task Logs (Last 2000 lines):*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{task_logs}```"}}
        ]
    }

    try:
        msg_body = json.dumps(msg).encode("utf-8")
        print(f"Message size: {len(msg_body)} bytes")
        print(f"Webhook URL: {SLACK_WEBHOOK_URL[:50]}...")
        
        response = http.request(
            "POST",
            SLACK_WEBHOOK_URL,
            body=msg_body,
            headers={"Content-Type": "application/json"}
        )

        print("Slack response status:", response.status)
        print("Slack response body:", response.data.decode("utf-8"))
        
        if response.status != 200:
            print(f"ERROR: Slack webhook returned {response.status}")
        
    except Exception as e:
        print(f"Error sending to Slack: {str(e)}")
