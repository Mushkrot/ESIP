"""
Read-only exploration of Dialogflow CX project.
"""
import os
from dotenv import load_dotenv
from google.cloud import dialogflowcx_v3 as df

load_dotenv()

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = CREDENTIALS

LOCATIONS = ["global", "us-central1", "us-east1", "us-west1", "europe-west1", "europe-west2", "asia-northeast1"]

print(f"Project: {PROJECT_ID}\n")
print("=" * 60)
print("Searching for Dialogflow CX agents across locations...")
print("=" * 60)

found_agents = []

for location in LOCATIONS:
    try:
        client = df.AgentsClient(
            client_options={"api_endpoint": f"{location}-dialogflow.googleapis.com"}
        )
        parent = f"projects/{PROJECT_ID}/locations/{location}"
        agents = list(client.list_agents(parent=parent))
        if agents:
            print(f"\n[{location}] Found {len(agents)} agent(s):")
            for agent in agents:
                found_agents.append((location, agent))
                print(f"  Name:         {agent.display_name}")
                print(f"  ID:           {agent.name.split('/')[-1]}")
                print(f"  Language:     {agent.default_language_code}")
                print(f"  Time zone:    {agent.time_zone}")
                print(f"  Full path:    {agent.name}")
                print()
    except Exception as e:
        if "403" in str(e) or "401" in str(e):
            print(f"  [{location}] Permission denied")
        elif "404" in str(e) or "not found" in str(e).lower():
            pass  # location not available, skip silently
        else:
            print(f"  [{location}] {type(e).__name__}: {e}")

if not found_agents:
    print("\nNo agents found in any location.")
else:
    print(f"\nTotal agents found: {len(found_agents)}")
