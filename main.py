from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from .config import EXCLUDED_TITLES
from .report.report import process_report

import os

app = FastAPI(root_path=os.getenv("ROOT_PATH", "/report"))

# Allow Grafana front-end to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ReportRequest(BaseModel):
    dashboard_url: str
    email_report: bool = False
    email_to: str = None

@app.post("/generate_report/")
async def generate_report(req: ReportRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_report, req.dashboard_url, req.email_to, excluded_titles=EXCLUDED_TITLES)
    return {"message": f"Report generation started for {req.email_to}"}
