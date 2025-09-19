from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from .config import EXCLUDED_TITLES
from .report import process_report

app = FastAPI(root_path="/report")

class ReportRequest(BaseModel):
    dashboard_url: str
    email_to: str = None

@app.post("/generate_report/")
async def generate_report(req: ReportRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_report, req.dashboard_url, req.email_to, excluded_titles=EXCLUDED_TITLES)
    return {"message": f"Report generation started for {req.email_to}"}
