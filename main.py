from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from .config import EXCLUDED_TITLES, PROMETHEUS_URL
from .report.report import process_report

import os
import logging

logging.basicConfig(
    level=logging.INFO,  # or DEBUG if you want more details
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

app = FastAPI(root_path=os.getenv("ROOT_PATH", "/report"))
logger.info("Configured direct Prometheus URL: %s", PROMETHEUS_URL)

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


@app.get("/metric", response_class=HTMLResponse)
@app.get("/metrics", response_class=HTMLResponse)
async def metrics_page():
        return """
<!doctype html>
<html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Grafana Reporter Metrics Endpoint</title>
        <style>
            body {
                font-family: "Segoe UI", Tahoma, sans-serif;
                margin: 0;
                background: linear-gradient(135deg, #f7fafc, #edf2f7);
                color: #1a202c;
            }
            .wrap {
                max-width: 760px;
                margin: 8vh auto;
                padding: 24px;
            }
            .card {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
                padding: 24px;
                box-shadow: 0 10px 28px rgba(15, 23, 42, 0.08);
            }
            h1 {
                margin-top: 0;
                font-size: 1.5rem;
            }
            code {
                background: #f1f5f9;
                padding: 2px 6px;
                border-radius: 6px;
            }
            .muted {
                color: #4a5568;
            }
            ul {
                margin: 0;
                padding-left: 1.25rem;
            }
        </style>
    </head>
    <body>
        <div class="wrap">
            <div class="card">
                <h1>Grafana Reporter</h1>
                <p class="muted">This pod is reachable at <code>/metric</code> and <code>/metrics</code>.</p>
                <p>Available API route:</p>
                <ul>
                    <li><code>POST /generate_report/</code> to trigger report generation</li>
                </ul>
            </div>
        </div>
    </body>
</html>
"""

@app.post("/generate_report/")
async def generate_report(req: ReportRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_report, req.dashboard_url, req.email_to, excluded_titles=EXCLUDED_TITLES)
    return {"message": f"Report generation started for {req.email_to}"}
