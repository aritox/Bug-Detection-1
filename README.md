<div align="center">
  <h1>AI Code Review Bot</h1>
  <p>
    Automate your Azure DevOps pull request reviews with the power of AI.
    <br/>
    Built using Flask, Groq LLMs, and Azure DevOps Service Hooks.
  </p>

  <p>
    <strong>Comment <code>/ai-bot</code> on a pull request, and let the AI do the review.</strong>
  </p>

  <br />

  <a href="#features">Features</a> |
  <a href="#quick-start">Quick Start</a> |
  <a href="#usage">Usage</a> |
  <a href="#tech-stack">Tech Stack</a> |
  <a href="#license">License</a>
</div>

---

![AI Bot Screenshot](./images/ai-bot.png)

---

## What is This?

`AI Code Review Bot` is a lightweight, Flask-based Azure DevOps integration that:

- Fetches the current pull request diff
- Analyzes it using Groq + custom rules
- Posts the review back as an Azure DevOps pull request discussion thread

This is useful for teams that want a manual `/ai-bot` trigger inside Azure DevOps instead of always-on review automation.

---

## Features

- Detects bug-risk issues using your `rules.txt`
- Reviews the current Azure DevOps pull request diff
- Posts the result as a PR thread comment
- Works with private Azure DevOps repos via PAT
- Keeps the existing Flask + Vercel deployment shape
- Supports the same `/ai-bot` trigger flow used before

---

## Quick Start

### 1. Clone the Repo

```bash
git clone https://github.com/Prashant-Bhapkar/ai-code-review.git
cd ai-code-review
```

### 2. Add Rules

Create your `rules.txt` with rules like:

```text
Do not use print statements for logging.
Always use 'with' when opening files.
Avoid hardcoded secrets.
```

### 3. Set Environment Variables

| Variable | Description |
| --- | --- |
| `GROQ_API_KEY` | Your Groq API key |
| `GROQ_MODEL` | Optional model override |
| `AZURE_DEVOPS_TOKEN` | Azure DevOps PAT with repo read and PR thread write access |
| `AZURE_DEVOPS_TRIGGER_COMMENT` | Optional trigger text. Defaults to `/ai-bot` |

### 4. Configure Azure DevOps

Create an Azure DevOps Service Hook for `ms.vss-code.git-pullrequest-comment-event` and point it to your deployed `/webhook` endpoint.

### 5. Deploy

You can host this using:

- [Vercel](https://vercel.com)
- Docker
- Your own Flask server

---

## Usage

1. Open a pull request in Azure DevOps.
2. Comment `/ai-bot` on the pull request.
3. Wait a few seconds.

The bot will:

- Fetch the current PR diff from Azure DevOps
- Run your rules and prompt through Groq
- Post a review thread back to the pull request

---

## Demo Output

```md
AI Code Review Feedback

Overall Risk Level: Medium

Risk Summary:
- Error handling changed around PR diff collection and may now fail closed
- Review thread posting depends on a valid Azure DevOps PAT
```

---

## Tech Stack

- Flask
- Groq LLM API
- Azure DevOps Service Hooks + REST API
- Vercel / Docker friendly deployment
- Unified diff generation from Azure DevOps item versions

---

## License

This project is licensed under the [Apache License 2.0](./LICENSE).
