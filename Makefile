.PHONY: deploy

# Pull main, restart the watcher and the dashboard, then update every worker to the same
# commit. Run in the checkout the live dashboard runs from. DEPLOY_ARGS=--force restarts
# even while cron jobs are running (they are recorded as interrupted).
deploy:
	python3 -I scripts/deploy.py $(DEPLOY_ARGS)
