# Career Dresden Kit

Daily job-search assistant for Dresden/Germany working-student software roles.

It fetches jobs, ranks them against Amandeep Singh's software-engineering profile, generates application packets for the best matches, and can email a morning brief with the packet attached.

## What It Sends

The email includes:

- Job title, company, location, source, posting date, and apply link
- Match score and reasons
- Job-description preview
- Manual search links for portals that are safer to check manually
- A ZIP attachment with generated application docs for the top matches

Each generated application packet contains:

- `cover_letter.md` - tailored cover-letter draft
- `tailored_resume_brief.md` - what to emphasize in the CV for that job
- `job_description.md` - saved job snapshot and link
- Base resume PDF if configured in `application.resume_pdf`

## Why It Does Not Auto-Apply

The tool prepares application material but does not submit applications automatically. Job applications often require truthful legal, immigration, availability, salary, and consent answers. Submitting without your final review is risky and can also violate portal terms.

## Sources

Current safe sources:

- Arbeitnow public API
- Workday candidate APIs for configured companies, starting with NXP
- Optional RSS feeds
- Manual links for SAP, Nokia, Vodafone, Infineon, LinkedIn, and Indeed

LinkedIn and Indeed are included as manual links because automated scraping can violate terms, trigger bot protection, or risk account restriction.

## Local Setup

Copy the example config:

```powershell
Copy-Item .\config.example.json .\config.local.json
```

Edit `config.local.json`:

- Set `email.enabled` to `true` when you want email delivery
- Set `email.from`, `email.to`, and `email.username`
- Keep your password out of the file
- Check `application.resume_pdf`

For Gmail, create an app password and set it in the environment:

```powershell
$env:JOB_AGENT_SMTP_PASSWORD = "your-gmail-app-password"
```

Run without sending email:

```powershell
python .\job_agent.py --config .\config.local.json --limit 12 --docs-limit 5
```

Run and send the morning email:

```powershell
python .\job_agent.py --config .\config.local.json --limit 12 --docs-limit 5 --email
```

Outputs:

```text
latest_job_matches.md
generated_applications/YYYY-MM-DD/amandeep_application_packets_YYYY-MM-DD.zip
```

## GitHub Actions Morning Email

The repository includes `.github/workflows/morning-job-brief.yml`.

Add these repository secrets:

```text
JOB_AGENT_EMAIL_FROM
JOB_AGENT_EMAIL_TO
JOB_AGENT_EMAIL_USERNAME
JOB_AGENT_SMTP_PASSWORD
```

Then run the workflow manually once from GitHub Actions. After that, it runs Monday-Friday in the morning.

## Add More Companies

Add more Workday sources in `config.local.json`:

```json
{
  "company": "Example Company",
  "base_url": "https://example.wd3.myworkdayjobs.com",
  "tenant": "example",
  "site": "careers",
  "query": "Dresden Working Student Software",
  "fetch_details": true
}
```

For non-Workday job boards, add RSS feeds if they provide them, or add a manual search link.

## Project Files

- `job_agent.py` - fetch, rank, generate docs, email
- `config.example.json` - safe example configuration
- `amandeep_resume_working_student.tex` - LaTeX resume source
- `application_packet/` - existing CV and cover-letter PDFs
- `cover_letters/` - existing LaTeX cover letters
- `.github/workflows/morning-job-brief.yml` - scheduled email workflow
