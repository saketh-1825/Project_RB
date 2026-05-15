from fastapi import FastAPI

from workflow.graph import build_graph


app = FastAPI(title="LangGraph SRE Copilot")

graph = build_graph()



@app.get("/api/v1/health")

async def health():

    return {

        "status": "ok",

        "active_analyses": 0,

        "queue_depth": 0
    }



@app.post("/api/v1/analyses")

async def start_analysis():

    initial_state = {

        "alert_id": "test_alert",

        "status": "running",

        "findings": [],

        "current_agent": None,

        "incident_id": None
    }


    result = graph.invoke(initial_state)


    return result