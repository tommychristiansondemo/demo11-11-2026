"""One-off Fargate task run by deploy.sh on a fresh environment: measure all Model Lab models
(apply=false) so the ranking shows real data from the start."""
import runtime

runtime.setup_logging()
import model_lab  # noqa: E402

if __name__ == "__main__":
    model_lab.evaluate({"username": "deploy-script"},
                       {"weights": {"quality": 50, "cost": 25, "latency": 25}, "apply": False})
