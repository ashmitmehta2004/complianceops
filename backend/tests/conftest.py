import os

# Hermetic tests: importing app.main must never read a developer's real backend/.env.
# Set at conftest import, which runs before any test module imports the app.
os.environ["COMPLIANCEOPS_ENV_FILE"] = ""
