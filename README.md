<div align="center">
  <h1>AI Code Review & Test Failure Predictor</h1>
  <p>
    Intelligent QA assistant for Azure DevOps that reviews code and predicts failing test cases before execution.
    <br/>
    Built with Python, FastAPI/Flask, Machine Learning, and Azure DevOps APIs.
  </p>

  <p>
    <strong>Automate code quality AND anticipate test failures directly from your pull requests.</strong>
  </p>

  <br />

  <a href="#features">Features</a> |
  <a href="#architecture">Architecture</a> |
  <a href="#quick-start">Quick Start</a> |
  <a href="#usage">Usage</a> |
  <a href="#evaluation">Evaluation</a> |
  <a href="#tech-stack">Tech Stack</a>
</div>

---

## 🚀 What is This?

This project is an **AI-powered QA platform** designed for Azure DevOps pipelines.

It combines two key capabilities:

### 1️⃣ AI Code Review
- Triggered manually via `/ai-bot` in pull requests
- Analyzes PR diffs using LLMs + custom rules
- Detects risky patterns, bad practices, and potential bugs
- Posts structured feedback directly in Azure DevOps threads

### 2️⃣ Test Failure Prediction
- Predicts which automated test cases are likely to fail BEFORE execution
- Uses historical test runs, PR metadata, and failure patterns
- Helps prioritize tests and reduce CI/CD feedback time

---

## ⚡ Features

### 🔍 Code Review
- Rule-based + LLM-powered PR analysis
- Custom `rules.txt` support
- Azure DevOps PR thread integration
- Manual `/ai-bot` trigger

### 🤖 Failure Prediction
- Predicts failures at **test-case level**
- Uses:
  - PR metadata (branch, commit, history)
  - Test execution history
  - Failure patterns
- Outputs:
  - Failure probability
  - Risk classification (Low / Medium / High)

### 📊 Evaluation Engine
- Supports **full-period evaluation (e.g. March 2026)**
- Handles:
  - PR ↔ test run time offsets
  - 72-hour matching fallback
- Strict handling of missing tests:
  - Not executed ≠ passed
- Computes metrics only on executed tests

### 🔗 Azure DevOps Integration
- Pull Requests
- Test Runs & Results
- Build metadata
- Service Hooks (for automation)

---

## 🧠 Architecture
