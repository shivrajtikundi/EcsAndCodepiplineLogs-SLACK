import json
import boto3
import urllib3
from datetime import datetime

codepipeline = boto3.client("codepipeline")
codebuild = boto3.client("codebuild")
ecs = boto3.client("ecs")
logs = boto3.client("logs")

http = urllib3.PoolManager()

SLACK_WEBHOOK_URL = "https://lona.webhook.office.com/webhookb2"
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

    # STEP 3 — send to Teams
    send_cp_to_teams(
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
    
    # Only send notifications for failed tasks
    if last_status not in ["STOPPED", "STOPPING"]:
        return {"statusCode": 200, "body": "Task not in failure state"}
    
    # Extract cluster from task ARN
    cluster = extract_cluster_from_task_arn(task_arn)
    
    print(f"Extracted Cluster: {cluster}")
    
    # STEP 1 — get task details
    task_name, container_name, service_name = get_ecs_task_details(cluster, task_arn)
    
    print(f"Task Name: {task_name}, Container: {container_name}")

    # STEP 2 — fetch logs
    task_logs = get_ecs_task_logs(cluster, task_arn, container_name) if container_name else "No container found."

    # STEP 3 — send to Teams
    send_ecs_to_teams(
        cluster=cluster,
        task_arn=task_arn,
        task_name=task_name,
        container_name=container_name,
        task_logs=task_logs,
        status=last_status,
    )

    return {"statusCode": 200, "body": "OK"}


# ==================================================
# HELPERS FOR EXTRACTION
# ==================================================
def extract_cluster_from_task_arn(task_arn):
    """Extract cluster name from task ARN"""
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

                    external_id = latest.get("externalExecutionId")
                    
                    print(f"External Execution ID: {external_id}")

                    build_id = None
                    if external_id:
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


def get_codebuild_logs(build_id):
    try:
        print(f"Fetching logs for build ID: {build_id}")
        
        build_response = codebuild.batch_get_builds(ids=[build_id])
        
        if not build_response.get("builds"):
            return f"No build found with ID: {build_id}"
        
        build = build_response["builds"][0]
        logs_info = build.get("logs", {})

        log_group = logs_info.get("groupName")
        log_stream = logs_info.get("streamName")

        if not log_group or not log_stream:
            return f"No CloudWatch logs found in build metadata."

        print("Auto-detected log group:", log_group)
        print("Auto-detected log stream:", log_stream)

        response = logs.get_log_events(
            logGroupName=log_group,
            logStreamName=log_stream,
            limit=3500,
            startFromHead=False
        )

        events = response.get("events", [])
        if not events:
            return "No log events found."

        all_logs = "\n".join(e["message"] for e in events)
        
        if len(all_logs) > 1500:
            all_logs = all_logs[:-1500]

        return all_logs

    except Exception as e:
        print(f"Error fetching CodeBuild logs: {str(e)}")
        return f"Error fetching CodeBuild logs: {str(e)}"


def get_ecs_task_details(cluster, task_arn):
    try:
        print(f"Fetching task details for: {task_arn}, Cluster: {cluster}")
        
        response = ecs.describe_tasks(
            cluster=cluster,
            tasks=[task_arn],
            include=['TAGS']
        )
        
        if not response.get("tasks"):
            return None, None, None
        
        task = response["tasks"][0]
        task_name = task_arn.split("/")[-1]
        container_name = None
        service_name = None
        
        if task.get("containers"):
            container_name = task["containers"][0].get("name")
        
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
        
        service_name = None
        if task.get("tags"):
            for tag in task["tags"]:
                if tag.get("key") == "aws:ecs:serviceName":
                    service_name = tag.get("value")
                    break
        
        if not service_name:
            service_name = container_name
        
        print(f"Service Name: {service_name}")
        
        if task.get("containers"):
            for container in task["containers"]:
                if container.get("name") == container_name:
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
        return all_logs[-2000:] if len(all_logs) > 2000 else all_logs
        
    except Exception as e:
        print(f"Error fetching ECS logs: {str(e)}")
        return f"Error fetching logs: {str(e)}"


# ==================================================
# TEAMS MESSAGE FORMATTERS
# ==================================================
def send_cp_to_teams(pipeline_name, execution_id, failed_stage, failed_action, build_logs, state):

    if len(build_logs) > 2000:
        build_logs = build_logs[-2000:]

    build_logs = build_logs.replace("\\", "\\\\").replace('"', '\\"')

    if state == "SUCCEEDED":
        msg = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": "CodePipeline Succeeded",
            "themeColor": "28a745",
            "sections": [
                {
                    "activityTitle": "✅ CodePipeline Succeeded",
                    "facts": [
                        {"name": "Pipeline", "value": pipeline_name},
                        {"name": "Status", "value": state},
                        {"name": "Execution ID", "value": execution_id},
                        {"name": "Time", "value": str(datetime.now())}
                    ]
                }
            ]
        }
    else:
        msg = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": "CodePipeline Failed",
            "themeColor": "dc3545",
            "sections": [
                {
                    "activityTitle": "🚨 CodePipeline Failed",
                    "facts": [
                        {"name": "Pipeline", "value": pipeline_name},
                        {"name": "Status", "value": state},
                        {"name": "Failed Stage", "value": str(failed_stage)},
                        {"name": "Failed Action", "value": str(failed_action)},
                        {"name": "Execution ID", "value": execution_id},
                        {"name": "Time", "value": str(datetime.now())}
                    ]
                },
                {
                    "activityTitle": "Build Logs (Last 3500 lines - 1500 chars)",
                    "text": f"```{build_logs}```"
                }
            ]
        }

    post_to_teams(msg)


def send_ecs_to_teams(cluster, task_arn, task_name, container_name, task_logs, status):

    if len(task_logs) > 1000:
        task_logs = task_logs[-1000:]

    task_logs = task_logs.replace("\\", "\\\\").replace('"', '\\"')

    msg = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": "ECS Task Failed",
        "themeColor": "dc3545",
        "sections": [
            {
                "activityTitle": "🚨 ECS Task Failed",
                "facts": [
                    {"name": "Cluster", "value": cluster},
                    {"name": "Task Name", "value": task_name},
                    {"name": "Container", "value": container_name},
                    {"name": "Status", "value": status},
                    {"name": "Task ARN", "value": task_arn},
                    {"name": "Time", "value": str(datetime.now())}
                ]
            },
            {
                "activityTitle": "Task Logs (Last 2000 lines)",
                "text": f"```{task_logs}```" if task_logs else "No logs available"
            }
        ]
    }

    post_to_teams(msg)


# ==================================================
# POST TO TEAMS
# ==================================================
def post_to_teams(msg):
    try:
        body = json.dumps(msg).encode("utf-8")
        print(f"Message size: {len(body)} bytes")

        response = http.request(
            "POST",
            SLACK_WEBHOOK_URL,
            body=body,
            headers={"Content-Type": "application/json"}
        )

        print("Teams response status:", response.status)
        print("Teams response body:", response.data.decode("utf-8"))

    except Exception as e:
        print(f"Error sending to Teams: {str(e)}")
