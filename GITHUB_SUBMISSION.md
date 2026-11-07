# GitHub Repository Setup and Submission Guide

**Project:** Ethical Loan Collections Intelligence Platform
**Challenge:** YottaFlex AI — Round 3

This document describes how to initialize Git, prepare the project, create a public GitHub repository, push the complete project, verify public access, and submit the repository link.

All commands in this document are written for **Windows Command Prompt (CMD)**.

---

## 1. Prerequisites

Before publishing the project, ensure that:

- Git for Windows is installed.
- Python 3.11.9 is installed.
- The project runs locally.
- The virtual environment is available.
- The README and project documentation are complete.
- No passwords, API keys, personal credentials, or private information are included in the repository.

Check Git installation:

```cmd
git --version
```

Check Python:

```cmd
python --version
```

---

## 2. Open the Project Directory

Open CMD and navigate to the project root:

```cmd
cd /d "D:\YottaFlex_AI_Round3_Ethical_Collections\ethical-collections-ai"
```

Activate the existing virtual environment:

```cmd
myvenv\Scripts\activate
```

Confirm the current directory:

```cmd
cd
```

---

## 3. Review Git Ignore Rules

Before adding files, inspect the `.gitignore` file:

```cmd
type .gitignore
```

Ensure that local and unnecessary files are excluded.

Recommended entries:

```gitignore
# Python
__pycache__/
*.py[cod]
*.pyo

# Virtual environments
venv/
.venv/
myvenv/
env/

# Environment variables and secrets
.env
.env.*
!.env.example

# Test and cache files
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/

# IDE and editor files
.vscode/
.idea/

# Operating system files
.DS_Store
Thumbs.db
desktop.ini

# Logs
*.log

# Temporary files
*.tmp
*.bak
```

Do not automatically exclude the synthetic dataset or required project reports. Review the challenge requirements and repository size before deciding what to publish.

Never commit real customer data, credentials, database passwords, or private environment files.

---

## 4. Check Project Files Before Staging

Review the project root:

```cmd
dir /a
```

Check the environment files:

```cmd
dir /a .env*
```

Review the data directory:

```cmd
dir data
```

Review the current Git status:

```cmd
git status --short
```

If the project contains an existing Git repository, inspect its history and remote configuration before changing anything.

---

## 5. Initialize Git

If Git has not already been initialized:

```cmd
git init
```

Set the main branch:

```cmd
git branch -M main
```

If the repository already exists, do not initialize it again unnecessarily.

---

## 6. Stage the Project Files

After reviewing `.gitignore` and confirming that no private files will be uploaded:

```cmd
git add .
```

Review the staged files:

```cmd
git status
```

Check the staged file list:

```cmd
git diff --cached --name-only
```

Check the staged changes:

```cmd
git diff --cached --stat
```

**Important:** Do not commit until the staged file list has been reviewed. Remove any accidentally staged private or unnecessary files before proceeding.

---

## 7. Create the First Commit

Once the staged files have been reviewed:

```cmd
git commit -m "Initial submission: Ethical Collections Intelligence Platform"
```

Verify the commit:

```cmd
git log -1 --oneline
```

Check the working tree:

```cmd
git status
```

The working tree should be clean if all changes have been committed.

---

## 8. Create a Public GitHub Repository

1. Open [https://github.com/](https://github.com/)

2. Sign in to your GitHub account.

3. Select **New repository**.

4. Enter a repository name:

   `ethical-collections-ai`

5. Add a description:

   `An explainable and ethical loan collections intelligence platform built with Python, FastAPI, SQLite, and Streamlit.`

6. Select **Public** visibility.

7. Do not initialize the repository with an additional README, `.gitignore`, or license because these files already exist in the local project.

8. Click **Create repository**.

Keep the newly created repository page open.

---

## 9. Connect the Local Repository to GitHub

Replace `YOUR_USERNAME` with your actual GitHub username.

```cmd
git remote add origin https://github.com/YOUR_USERNAME/ethical-collections-ai.git
```

Verify the remote:

```cmd
git remote -v
```

If an `origin` remote already exists, inspect it first. Update it only if it points to the wrong repository.

---

## 10. Push the Project to GitHub

Push the main branch:

```cmd
git push -u origin main
```

GitHub may ask you to authenticate through your browser or another supported authentication method.

Never paste your GitHub password or personal access token into a public file, source code, or chat.

Wait until Git confirms that the push has completed successfully.

---

## 11. Verify the Public Repository

Open your repository:

```text
https://github.com/YOUR_USERNAME/ethical-collections-ai
```

Check the following:

- Repository visibility is Public.
- README.md is displayed correctly.
- Project source code is visible.
- Required documentation is present.
- Synthetic data and required reports are available.
- No private credentials or environment files are exposed.
- No unnecessary virtual environment or cache files are uploaded.

### Verify using a private browser window

1. Open an Incognito or InPrivate browser window.
2. Paste the public GitHub repository URL.
3. Open the repository without signing in.
4. Confirm that the repository and required files are accessible.

A repository that is public and accessible without authentication satisfies the public-access verification requirement.

---

## 12. Updating the Repository

Whenever project changes are made after the first push, review and commit them:

```cmd
git status
git add .
git status
git commit -m "Update project documentation and implementation"
git push
```

Review staged files before every commit.

---

## 13. Final Submission Checklist

Before submitting the challenge:

- [x] Git repository initialized.
- [x] Main branch named `main`.
- [ ] `.gitignore` reviewed.
- [ ] No credentials or private files staged.
- [ ] README.md included.
- [ ] GITHUB_SUBMISSION.md included.
- [ ] PROMPT_HISTORY.md included.
- [ ] AI disclosure included.
- [ ] Source code included.
- [ ] Required synthetic data included.
- [ ] Test and validation reports included.
- [ ] Changes committed successfully.
- [ ] GitHub repository created.
- [ ] Repository visibility set to Public.
- [ ] Project pushed successfully.
- [ ] Repository opened in Incognito mode.
- [ ] Public repository link copied.
- [ ] Repository link submitted through the YottaFlex AI recruitment portal.

---

## 14. Challenge Submission Details

**Challenge:** YottaFlex AI — Round 3

**Project:** Ethical Loan Collections Intelligence Platform

**GitHub Repository:**
`https://github.com/YOUR_USERNAME/ethical-collections-ai`

**Repository Visibility:** Public

**Final Verification:** Public repository access tested using an Incognito browser window.

**Submission Portal:**
[https://recruitment.yottaflex.ai/careers/my/application/7866/round2](https://recruitment.yottaflex.ai/careers/my/application/7866/round2)

**Deadline:** November 7, 2026, 10:06:44 PM IST.

Replace the repository URL placeholder with the actual public repository URL before submitting.

---

## 15. Important Security and Submission Notes

- Never upload `.env` files containing real secrets.
- Never upload GitHub authentication tokens.
- Never upload private customer or production data.
- Keep the synthetic challenge dataset separate from real-world data.
- Do not claim that the repository is public until Incognito verification succeeds.
- Do not claim that a feature works unless it has been implemented and verified.
- Preserve the project source code and documentation in one repository.

**Final deliverable:** One publicly accessible GitHub repository containing the complete Ethical Loan Collections Intelligence Platform and its supporting documentation.
