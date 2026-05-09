# Deploy notes

## Railway (recommended for hackathon)

```bash
# from code/transition-pilot/
railway login
railway init    # link to a new project
railway variables set ANTHROPIC_API_KEY=sk-ant-...
railway variables set ANTHROPIC_MODEL=claude-haiku-4-5-20251001
railway up
```

The healthcheck path is `/health`. Verify after deploy:

```bash
curl https://<your-domain>.up.railway.app/health
```

## Cloud Run

```bash
gcloud run deploy transition-pilot \
  --source . \
  --port 8089 \
  --set-env-vars "ANTHROPIC_API_KEY=sk-ant-...,ANTHROPIC_MODEL=claude-haiku-4-5-20251001" \
  --allow-unauthenticated \
  --region us-central1 \
  --memory 512Mi \
  --cpu 1
```

## Marketplace publish (Prompt Opinion)

After Railway returns a public HTTPS URL, paste that URL into the Prompt Opinion
marketplace MCP server form. Marketplace publication is a hackathon submission
requirement — do this BEFORE recording the demo so the demo can show the published
asset.
