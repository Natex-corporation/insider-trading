# Portfolio & Roadmap Entry: Insider Edge

This document contains pre-formatted snippets and metadata for adding **Insider Edge** to your portfolio site, CVUT Crossroad roadmap table, or personal resume.

---

## Project Summary Card

- **Project Name**: Insider Edge (Insider Trading Bot)
- **Tagline**: Automated SEC Form 4 insider trading bot with safety guardrails, SQLite state engine, and real-time web control room.
- **Repository**: [https://github.com/Natex-corporation/insider-trading](https://github.com/Natex-corporation/insider-trading)
- **Category**: FinTech / Algorithmic Trading / DevOps
- **Status**: Complete & Maintained (v1.0.0)
- **Tech Stack**: `Python 3.11`, `Alpaca API`, `Starlette`, `SQLite`, `Docker`, `TrueNAS SCALE`, `Prometheus`, `GitHub Actions`
- **Key Highlights**:
  - Automated SEC Form 4 scraping & deduplication
  - Multi-tier portfolio risk guardrails & exposure limits
  - Deterministic order execution & fill-based TP/SL exits
  - Embedded real-time web dashboard & Prometheus metrics
  - Hardened non-root Docker container & TrueNAS SCALE automation

---

## 1. Markdown Roadmap Table Row

Use this in any Markdown-based roadmap (e.g. `ROADMAP.md` or portfolio README):

```markdown
| Project | Domain | Tech Stack | Status | Repository | Key Features |
|---|---|---|:---:|:---:|---|
|**[Insider Edge](https://github.com/Natex-corporation/insider-trading)** | FinTech & Automation | Python, Alpaca API, SQLite, Docker, Prometheus | ✅ Completed (v1.0.0) | [GitHub](https://github.com/Natex-corporation/insider-trading) | Form 4 scraping, risk management, live control room dashboard, TrueNAS deploy |
```

---

## 2. CVUT Crossroad Style HTML Roadmap Table Row

This HTML snippet uses CSS tokens compatible with `cvutcrossroads` (`styles.css`):

```html
<tr class="roadmap-row">
  <td class="roadmap-project">
    <a href="https://github.com/Natex-corporation/insider-trading" target="_blank" rel="noopener noreferrer" class="project-title-link">
      <strong>Insider Edge</strong>
    </a>
    <span class="project-desc">Automated SEC Form 4 insider trading bot with risk engine & live web dashboard.</span>
  </td>
  <td class="roadmap-category">
    <span class="badge badge-accent-blue">FinTech / Algorithmic Trading</span>
  </td>
  <td class="roadmap-tech">
    <span class="tech-tag">Python 3.11</span>
    <span class="tech-tag">Alpaca API</span>
    <span class="tech-tag">SQLite</span>
    <span class="tech-tag">Docker</span>
    <span class="tech-tag">Prometheus</span>
  </td>
  <td class="roadmap-status">
    <span class="status-pill status-completed">Completed (v1.0.0)</span>
  </td>
  <td class="roadmap-links">
    <a href="https://github.com/Natex-corporation/insider-trading" target="_blank" rel="noopener noreferrer" class="button-link">GitHub</a>
  </td>
</tr>
```

---

## 3. CVUT Crossroad Style Card Component

For grid or card-based roadmap / showcase views on `cvut-crossroad.com`:

```html
<article class="project-card">
  <div class="project-card-header">
    <span class="project-badge">FinTech & Trading</span>
    <span class="status-indicator live">v1.0.0</span>
  </div>
  <h3 class="project-title">
    <a href="https://github.com/Natex-corporation/insider-trading" target="_blank" rel="noopener noreferrer">
      Insider Edge
    </a>
  </h3>
  <p class="project-summary">
    Production-hardened Python trading service that tracks SEC Form 4 insider transactions, evaluates
    portfolio exposure guardrails, executes paper trades via Alpaca, and serves a live operations dashboard with Prometheus metrics.
  </p>
  <div class="project-tags">
    <span>Python 3.11</span>
    <span>Alpaca API</span>
    <span>SQLite ACID</span>
    <span>Docker Non-Root</span>
    <span>TrueNAS SCALE</span>
    <span>Prometheus</span>
  </div>
  <div class="project-actions">
    <a href="https://github.com/Natex-corporation/insider-trading" target="_blank" rel="noopener noreferrer" class="link-button">
      View Source on GitHub &#8594;
    </a>
  </div>
</article>
```

---

## 4. JSON Roadmap / Catalog Entry

If your site loads projects dynamically from a JSON data source:

```json
{
  "id": "insider-edge",
  "title": "Insider Edge",
  "category": "FinTech / Trading Automation",
  "status": "completed",
  "version": "1.0.0",
  "description": "Automated SEC Form 4 insider trading bot with risk guardrails, SQLite state management, and real-time operations dashboard.",
  "technologies": ["Python", "Alpaca API", "SQLite", "Docker", "TrueNAS SCALE", "Prometheus", "Starlette"],
  "repositoryUrl": "https://github.com/Natex-corporation/insider-trading",
  "featured": true,
  "date": "2026-08"
}
```
