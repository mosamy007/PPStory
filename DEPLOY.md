# Deployment Guide for Render.com

## Prerequisites
1. **GitHub Account**: You need a GitHub account.
2. **Render Account**: Create a free account at [render.com](https://render.com).

## Step 1: Push Code to GitHub
1. Initialize a git repository if you haven't already:
   ```bash
   git init
   git add .
   git commit -m "Initial commit for deployment"
   ```
2. Create a new repository on GitHub.
3. Push your code:
   ```bash
   git remote add origin <your-github-repo-url>
   git push -u origin main
   ```

## Step 2: Deploy on Render
1. Log in to your Render dashboard.
2. Click **New +** and select **Web Service**.
3. Connect your GitHub account and select the repository you just created.
4. **Configure the Service**:
   - **Name**: Choose a name (e.g., `video-editor-app`).
   - **Region**: Select the one closest to you (e.g., `Frankfurt` or `Ohio`).
   - **Runtime**: Select **Docker**.
   - **Instance Type**: Select **Free**.
5. **Environment Variables**:
   - You shouldn't need any special ones, but if you want to set `FLASK_ENV` to `production`, you can.
6. Click **Create Web Service**.

## Monitor Deployment
- Render will start building your Docker image. This might take 5-10 minutes because it needs to install `ffmpeg` and dependencies.
- Watch the logs. Once it says "Your service is live", click the URL at the top.

## Troubleshooting
- **"Out of Memory"**: If the build fails or the app crashes during video processing, it's likely due to the 512MB RAM limit on the free tier. Try processing shorter videos.
- **"Application Error"**: Check the "Logs" tab in Render to see the Python error stack trace.
