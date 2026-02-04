# ✨ Aura - The Elegant Code Reviewer

Aura is a sophisticated GitLab MR code review bot powered by local LLMs via **Ollama**. Unlike generic bots, Aura is designed to be a graceful and insightful collaborator, focusing on what truly matters: correctness, performance, and maintainability.

## 🌟 Why Aura?

- **Intelligent Deduplication**: Aura remembers her past advice. She scans entire discussion threads and normalizes text to ensure she never repeats herself.
- **Thread-Aware Replies**: If she has more to say on an existing topic, she replies gracefully to the thread rather than starting a new one.
- **Production Ready**: Built-in support for `.env` configuration and secure Git handling.
- **Dynamic Rules**: Aura can fetch rules from your project (`aura_rules.md`), a remote URL, or a local fallback.

## 🚀 Getting Started

### 1. Installation

Aura requires Python 3.10+ and the `requests` library.

```bash
pip install -r requirements.txt
```

### 2. Configuration

Aura is discreet. She keeps her secrets in a `.env` file. Copy the template to get started:

```bash
cp .env.template .env
```

Open `.env` and fill in your details:
- **GITLAB_TOKEN**: Your Personal Access Token.
- **MR_URL**: The full URL of the Merge Request to review.
- **MR_RULES_URL**: (Optional) A central URL for your review standards (e.g., Confluence or raw Git link).
- **OLLAMA_MODEL**: Aura prefers `qwen2.5:7b-16k` for her deep insights.

### 3. Usage

Simply run the launch script:

```bash
./run.sh
```

Aura will gracefully parse the MR, analyze the diffs, and provide her insights directly on the lines of code that need her attention.

## 🛠 Advanced Options

Aura can be customized to fit your needs via the command line or environment variables:

- **--resolve-fixed**: Aura will automatically resolve her old discussions if she sees the issue has been addressed.
- **--repo-type**: Choose between `standalone` or `package` rules (or let Aura guess automatically).
- **--dry-run**: See what Aura is thinking without her posting anything to GitLab.

## 📜 Metadata Tracking

Aura uses elegant, hidden Markdown markers to track her discussions:
`[//]: # (aura-review-bot anchor=... id=...)`

She maintains backwards compatibility with legacy `wm-ollama-review-bot` markers, so she won't duplicate comments from her previous identity.

---
*Gracefully reviewed by Aura*
