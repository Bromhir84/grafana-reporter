import logging
import yaml
import re
import os
import pandas as pd
from datetime import datetime
from .prometheus_utils import query_prometheus_range

logger = logging.getLogger(__name__)


class RecordingRuleBackfill:
    def __init__(self, yaml_path=None):
        if yaml_path is None:
            yaml_path = os.path.join(os.path.dirname(__file__), "runai.yaml")

        if not os.path.exists(yaml_path):
            raise FileNotFoundError(f"Recording rules YAML not found: {yaml_path}")

        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)

        self.rules_map = {}
        items = data.get("items", [])
        for item in items:
            spec = item.get("spec", {})
            for group in spec.get("groups", []):
                for rule in group.get("rules", []):
                    if "record" in rule and "expr" in rule:
                        self.rules_map[rule["record"]] = rule["expr"]

        logger.info(f"Loaded {len(self.rules_map)} recording rules from {yaml_path}")

    def _find_dependencies(self, expr: str):
        deps = []
        for rule in self.rules_map.keys():
            if re.search(rf"\b{re.escape(rule)}\b", expr):
                deps.append(rule)
        return deps

    def recompute_rule_for_timeframe(self, rule_name: str, start: datetime, end: datetime, step: int = 3600):
        if rule_name not in self.rules_map:
            logger.warning(f"No recording rule found for {rule_name}")
            return pd.DataFrame(columns=["project", "department", rule_name])

        expr = self.rules_map[rule_name]
        deps = self._find_dependencies(expr)

        if not deps:
            # Raw-level expression -> query Prometheus directly
            logger.debug(f"Querying raw metric for {rule_name}: {expr}")
            resp = query_prometheus_range(expr, start, end, step)
            return self._prometheus_result_to_df(resp, rule_name)

        # Resolve dependencies recursively
        dep_dfs = {}
        for dep in deps:
            dep_dfs[dep] = self.recompute_rule_for_timeframe(dep, start, end, step)

        # Combine first dependency (simple handling)
        df = dep_dfs[deps[0]].copy()
        df[rule_name] = df[deps[0]]
        df.drop(columns=deps[0], inplace=True)

        # Handle division by scalar in expr
        match = re.search(r"/\s*([\d\.]+)", expr)
        if match:
            divisor = float(match.group(1))
            df[rule_name] = df[rule_name] / divisor

        return df

    def _prometheus_result_to_df(self, results, column_name):
        rows = []
        for r in results.get("data", {}).get("result", []):
            metric_labels = r.get("metric", {})
            project = metric_labels.get("project", "unknown")
            department = metric_labels.get("department", "unknown")
            if r.get("values"):
                _, value = r["values"][-1]
                rows.append({"project": project, "department": department, column_name: float(value)})
        if not rows:
            rows.append({"project": "unknown", "department": "unknown", column_name: 0.0})
        return pd.DataFrame(rows)

    def backfill_rule(self, record_name, start, end, step=3600):
        """
        Wrapper for recursive backfill that returns Prometheus-style dict.
        """
        df = self.recompute_rule_for_timeframe(record_name, start, end, step)
        result = []
        for _, row in df.iterrows():
            result.append({
                "metric": {"project": row["project"], "department": row["department"]},
                "values": [[start.timestamp(), row[record_name]]]
            })
        return {"data": {"result": result}}

    def backfill_rule_recursive(self, record_name, start, end, step, visited=None):
        if visited is None:
            visited = set()

        if record_name in visited:
            logger.warning(f"Already visited {record_name}, skipping recursion")
            return {"data": {"result": []}}

        if record_name not in self.rules_map:
            logger.warning(f"No expression found for recording rule: {record_name}")
            return {"data": {"result": []}}

        visited.add(record_name)
        expr = self.rules_map[record_name]

        # Detect other recording rules used in this expr
        tokens = re.findall(r"[a-zA-Z0-9_:]+", expr)
        dependency_rules = [tok for tok in tokens if tok in self.rules_map and tok != record_name]

        # Recursively backfill dependencies first
        for dep in dependency_rules:
            logger.info(f"Backfilling dependency {dep} for {record_name}")
            self.backfill_rule_recursive(dep, start, end, step, visited=visited)

        # Now backfill this rule itself
        logger.info(f"Backfilling rule: {record_name}")
        results = self.backfill_rule(record_name=record_name, start=start, end=end, step=step)
        return results
