# api/index.py
from app import app

# Vercel expects a handler named 'app' or 'application'
# This re-exports the Flask app for Vercel's serverless runtime
application = app

# For local development
if __name__ == "__main__":
    app.run()