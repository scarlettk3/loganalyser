#!/usr/bin/env python3
import os
import time
import json
import logging
import datetime
import requests
from kubernetes import client, config, watch
import concurrent.futures
import traceback
import queue
import threading

# Setup logging
logging.basicConfig(
    level=logging.INFO,  # Logging level set to INFO
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Ensures logs go to stdout for kubectl logs
        logging.FileHandler('/app/output/k8s_error_agent.log')  # Optional file logging
    ]
)
logger = logging.getLogger("k8s-error-agent")

# Configuration settings
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))  # In seconds
MAX_CONCURRENT_REQUESTS = 2  # Limit concurrent AI analysis requests
REQUEST_DELAY = 5  # Seconds between requests to avoid rate limiting

# Ensure output directory exists
os.makedirs(OUTPUT_DIR, exist_ok=True)

def sanitize_text(text, max_length=2000):
    """Sanitize and truncate text to prevent oversized requests"""
    if text is None:
        return ""
    try:
        # Sanitize and truncate
        sanitized = text.encode("utf-8", errors="replace").decode("utf-8")
        return sanitized[:max_length]
    except Exception as e:
        logger.error(f"Error sanitizing text: {e}")
        return str(text)[:max_length]

class K8sErrorAnalysisAgent:
    def __init__(self):
        try:
            # Try to load in-cluster config (when running inside a pod)
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            # Fall back to local config (for testing)
            config.load_kube_config()
            logger.info("Loaded local Kubernetes configuration")

        self.v1 = client.CoreV1Api()
        self.already_analyzed_pods = set()

    def get_all_namespaces(self):
        """Get all namespaces in the cluster"""
        namespaces = self.v1.list_namespace()
        return [ns.metadata.name for ns in namespaces.items]

    def get_failed_pods(self, namespace):
        """Get all pods with non-successful status in a specific namespace"""
        pods = self.v1.list_namespaced_pod(namespace=namespace)
        failed_pods = []

        for pod in pods.items:
            # Check if pod is in failed state
            pod_status = pod.status.phase
            container_statuses = pod.status.container_statuses if pod.status.container_statuses else []

            # Check if pod is in a failed/error state
            if (pod_status in ["Failed", "Unknown"] or
                    any(cs.state.waiting and cs.state.waiting.reason in ["CrashLoopBackOff", "Error", "ErrImagePull", "ImagePullBackOff"]
                        for cs in container_statuses) or
                    any(cs.state.terminated and cs.state.terminated.exit_code != 0
                        for cs in container_statuses)):

                pod_key = f"{namespace}/{pod.metadata.name}"
                if pod_key not in self.already_analyzed_pods:
                    logger.info(f"Found failed pod: {pod_key}")
                    failed_pods.append(pod)

        return failed_pods

    def get_pod_logs(self, pod, namespace):
        """Get logs from all containers in a pod"""
        logs = {}

        try:
            # If the pod has containers, get logs for each one
            if pod.spec.containers:
                for container in pod.spec.containers:
                    try:
                        container_log = self.v1.read_namespaced_pod_log(
                            name=pod.metadata.name,
                            namespace=namespace,
                            container=container.name,
                            tail_lines=50  # Reduced from 100 to limit payload size
                        )
                        logs[container.name] = sanitize_text(container_log)
                    except Exception as e:
                        logs[container.name] = f"Error retrieving logs: {str(e)}"

            # Also get pod events which can be helpful for debugging
            field_selector = f"involvedObject.name={pod.metadata.name}"
            events = self.v1.list_namespaced_event(
                namespace=namespace,
                field_selector=field_selector
            )
            events_text = "\n".join([
                f"{event.last_timestamp}: {event.reason} - {event.message}"
                for event in events.items
            ])
            logs["events"] = sanitize_text(events_text)

        except Exception as e:
            logger.error(f"Error getting logs for pod {pod.metadata.name}: {str(e)}")
            logs["error"] = str(e)

        return logs

    def create_ai_prompt(self, pod, namespace, logs):
        """Create a more concise prompt for AI analysis"""
        # Prepare the pod status information
        pod_status = pod.status.phase
        container_statuses = []
        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                status_info = {
                    "container": cs.name,
                    "restart_count": cs.restart_count
                }

                if cs.state.waiting:
                    status_info["state"] = "waiting"
                    status_info["reason"] = sanitize_text(cs.state.waiting.reason or "Unknown")
                elif cs.state.terminated:
                    status_info["state"] = "terminated"
                    status_info["exit_code"] = cs.state.terminated.exit_code
                    status_info["reason"] = sanitize_text(cs.state.terminated.reason or "Unknown")
                elif cs.state.running:
                    status_info["state"] = "running"

                container_statuses.append(status_info)

        # Compile log data for all containers
        log_text = ""
        for container_name, container_log in logs.items():
            log_text += f"\n\n===== {container_name} Logs =====\n{container_log}"

        # Create a more concise prompt
        prompt = f"""
        Analyze Kubernetes Pod Error:
        - Pod: {pod.metadata.name}
        - Namespace: {namespace}
        - Status: {pod_status}
        
        Container Statuses:
        {json.dumps(container_statuses, indent=2)}
        
        Logs Summary:
        {log_text}
        
        Provide a concise analysis:
        1. What is the primary error?
        2. Potential root cause
        3. Recommended action
        """
        return prompt

    def send_to_mistral(self, prompt):
        """Send prompt to Mistral AI with rate limiting"""
        if not MISTRAL_API_KEY:
            logger.error("MISTRAL_API_KEY environment variable not set")
            return {"success": False, "error": "Mistral API key not configured"}

        headers = {
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Content-Type": "application/json"
        }

        data = {
            "model": "mistral-small-latest",  # Using smaller model to reduce costs/rate limiting
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 300  # Limit response size
        }

        try:
            response = requests.post(MISTRAL_API_URL, headers=headers, json=data, timeout=30)
            response.raise_for_status()

            response_json = response.json()
            analysis = response_json['choices'][0]['message']['content']
            return {"success": True, "analysis": analysis}

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error calling Mistral AI API: {e}")
            return {"success": False, "error": f"Network error: {str(e)}"}
        except Exception as e:
            logger.error(f"Unexpected error calling Mistral AI API: {e}")
            return {"success": False, "error": str(e)}

    def process_pod(self, pod, namespace):
        """Process a single failed pod"""
        pod_key = f"{namespace}/{pod.metadata.name}"

        try:
            logger.info(f"Processing failed pod: {pod_key}")

            # Get pod logs
            logs = self.get_pod_logs(pod, namespace)

            # Create AI prompt
            prompt = self.create_ai_prompt(pod, namespace, logs)

            # Send to Mistral AI for analysis
            result = self.send_to_mistral(prompt)

            if result.get("success", False):
                analysis = result["analysis"]

                # Log the analysis (will appear in kubectl logs)
                logger.info("\n" + "="*80)
                logger.info(f"ANALYSIS FOR POD: {pod.metadata.name} IN NAMESPACE: {namespace}")
                logger.info("="*80)
                logger.info(analysis)
                logger.info("="*80 + "\n")

                # Mark as analyzed to avoid duplication
                self.already_analyzed_pods.add(pod_key)
            else:
                logger.error(f"Failed to analyze pod {pod_key}: {result.get('error', 'Unknown error')}")

        except Exception as e:
            logger.error(f"Error processing pod {pod_key}: {str(e)}")
            logger.error(traceback.format_exc())

    def run(self):
        """Main loop to continuously check for failed pods"""
        logger.info("Starting Kubernetes Error Analysis Agent")

        while True:
            try:
                namespaces = self.get_all_namespaces()
                logger.info(f"Found {len(namespaces)} namespaces")

                # Process pods with controlled concurrency
                with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as executor:
                    futures = []
                    for namespace in namespaces:
                        failed_pods = self.get_failed_pods(namespace)
                        logger.info(f"Found {len(failed_pods)} failed pods in namespace {namespace}")

                        # Process each failed pod with a delay between submissions
                        for pod in failed_pods:
                            futures.append(executor.submit(self.process_pod, pod, namespace))
                            time.sleep(REQUEST_DELAY)  # Delay between pod analysis requests

                    # Wait for all futures to complete
                    concurrent.futures.wait(futures)

                logger.info(f"Sleeping for {CHECK_INTERVAL} seconds before next check")
                time.sleep(CHECK_INTERVAL)

            except Exception as e:
                logger.error(f"Error in main loop: {str(e)}")
                logger.error(traceback.format_exc())
                time.sleep(10)  # Short sleep before retry on error

if __name__ == "__main__":
    agent = K8sErrorAnalysisAgent()
    agent.run()