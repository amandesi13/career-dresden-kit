import argparse
import base64
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree


USER_AGENT = "career-dresden-kit/0.2 (+personal job alerts; respectful fetching)"


@dataclass
class Job:
    source: str
    company: str
    title: str
    location: str
    url: str
    posted: str = ""
    description: str = ""
    score: int = 0
    reasons: tuple[str, ...] = ()


def fetch_json(url: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,text/html"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_arbeitnow() -> list[Job]:
    jobs: list[Job] = []
    data = fetch_json("https://www.arbeitnow.com/api/job-board-api")
    for item in data.get("data", []):
        jobs.append(
            Job(
                source="Arbeitnow",
                company=item.get("company_name", "Unknown"),
                title=clean_text(item.get("title", "")),
                location=clean_text(item.get("location", "")),
                url=item.get("url", ""),
                posted=format_posted(item.get("created_at", "")),
                description=html_to_text(item.get("description", "")),
            )
        )
    return jobs


def fetch_workday(source: dict[str, Any]) -> list[Job]:
    base_url = source["base_url"].rstrip("/")
    tenant = source["tenant"]
    site = source["site"]
    query = source.get("query", "")
    endpoint = f"{base_url}/wday/cxs/{tenant}/{site}/jobs"
    payload = {"limit": int(source.get("limit", 25)), "offset": 0, "searchText": query}
    try:
        data = fetch_json(endpoint, method="POST", payload=payload)
    except HTTPError as exc:
        if os.name != "nt" or exc.code != 400:
            raise
        data = fetch_json_with_powershell(endpoint, payload)

    jobs: list[Job] = []
    for item in data.get("jobPostings", []):
        external_path = item.get("externalPath", "")
        bullet_fields = item.get("bulletFields", [])
        description = " ".join(str(field) for field in bullet_fields) if isinstance(bullet_fields, list) else str(bullet_fields)
        description = clean_text(description)
        detail_url = f"{base_url}/wday/cxs/{tenant}/{site}{external_path}" if external_path else ""
        if detail_url and source.get("fetch_details", True):
            description = enrich_workday_description(detail_url, description)

        jobs.append(
            Job(
                source=f"Workday: {source.get('company', tenant)}",
                company=source.get("company", tenant),
                title=clean_text(item.get("title", "")),
                location=clean_text(item.get("locationsText", "")),
                url=urljoin(base_url, external_path),
                posted=item.get("postedOn", ""),
                description=description,
            )
        )
    return jobs


def enrich_workday_description(detail_url: str, fallback: str) -> str:
    try:
        detail = fetch_json(detail_url)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return fallback
    info = detail.get("jobPostingInfo", {})
    parts = [
        info.get("title", ""),
        info.get("jobDescription", ""),
        info.get("qualifications", ""),
        info.get("responsibilities", ""),
    ]
    text = html_to_text(" ".join(str(part) for part in parts if part))
    return text or fallback


def fetch_rss(source: dict[str, Any]) -> list[Job]:
    xml_text = fetch_text(source["url"])
    root = ElementTree.fromstring(xml_text)
    jobs: list[Job] = []
    for item in root.findall(".//item")[: int(source.get("limit", 30))]:
        title = clean_text(find_xml_text(item, "title"))
        link = clean_text(find_xml_text(item, "link"))
        description = html_to_text(find_xml_text(item, "description"))
        pub_date = clean_text(find_xml_text(item, "pubDate"))
        company = source.get("company", source.get("name", "RSS"))
        jobs.append(
            Job(
                source=f"RSS: {source.get('name', company)}",
                company=company,
                title=title,
                location=source.get("location", ""),
                url=link,
                posted=pub_date,
                description=description,
            )
        )
    return jobs


def find_xml_text(node: ElementTree.Element, tag: str) -> str:
    child = node.find(tag)
    return child.text if child is not None and child.text else ""


def fetch_json_with_powershell(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    encoded_body = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    command = (
        "$headers=@{'Content-Type'='application/json';'Accept'='application/json'}; "
        f"$body=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_body}')); "
        f"Invoke-RestMethod -Method Post -Uri '{url}' -Headers $headers -Body $body | ConvertTo-Json -Depth 12"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        timeout=40,
    )
    return json.loads(result.stdout)


def score_job(job: Job, config: dict[str, Any]) -> Job:
    candidate = config["candidate"]
    positive = [kw.lower() for kw in candidate.get("positive_keywords", [])]
    negative = [kw.lower() for kw in candidate.get("negative_keywords", [])]
    required_any = [kw.lower() for kw in candidate.get("required_any_keywords", [])]
    locations = [loc.lower() for loc in candidate.get("target_locations", [])]

    haystack = " ".join([job.title, job.company, job.location, job.description]).lower()
    location_text = job.location.lower()
    score = 0
    reasons: list[str] = []

    for keyword in positive:
        if keyword_matches(keyword, haystack):
            score += 6
            reasons.append(f"+{keyword}")

    for keyword in negative:
        if keyword_matches(keyword, haystack):
            score -= 14
            reasons.append(f"-{keyword}")

    if any(location and location != "germany" and location in location_text for location in locations):
        score += 15
        reasons.append("+location")

    if "working student" in haystack or "werkstudent" in haystack:
        score += 20
        reasons.append("+student-role")

    if "dresden" in haystack:
        score += 12
        reasons.append("+dresden")

    if required_any and not any(keyword_matches(keyword, haystack) for keyword in required_any):
        score -= 25
        reasons.append("-not-tech-focused")

    job.score = score
    job.reasons = tuple(reasons[:12])
    return job


def keyword_matches(keyword: str, text: str) -> bool:
    escaped = re.escape(keyword)
    if re.fullmatch(r"[a-z0-9+#/.-]+", keyword):
        return re.search(rf"(?<![a-z0-9+#/.-]){escaped}(?![a-z0-9+#/.-])", text) is not None
    return keyword in text


def collect_jobs(config: dict[str, Any]) -> tuple[list[Job], list[str]]:
    jobs: list[Job] = []
    warnings: list[str] = []
    sources = config.get("sources", {})

    if sources.get("arbeitnow"):
        add_source_jobs(jobs, warnings, "Arbeitnow", fetch_arbeitnow)

    for source in sources.get("workday", []):
        add_source_jobs(jobs, warnings, f"Workday {source.get('company', '')}".strip(), lambda source=source: fetch_workday(source))

    for source in sources.get("rss", []):
        add_source_jobs(jobs, warnings, f"RSS {source.get('name', '')}".strip(), lambda source=source: fetch_rss(source))

    unique: dict[str, Job] = {}
    for job in jobs:
        if job.url:
            unique[job.url] = score_job(job, config)

    minimum_score = int(config.get("candidate", {}).get("minimum_score", -999))
    ranked = sorted(
        (found_job for found_job in unique.values() if found_job.score >= minimum_score),
        key=lambda found_job: found_job.score,
        reverse=True,
    )
    return ranked, warnings


def add_source_jobs(jobs: list[Job], warnings: list[str], name: str, fetcher: Any) -> None:
    try:
        jobs.extend(fetcher())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, KeyError, ElementTree.ParseError, subprocess.SubprocessError) as exc:
        warnings.append(f"{name} failed: {exc}")


def render_markdown(jobs: list[Job], config: dict[str, Any], warnings: list[str], limit: int, packet_zip: Path | None) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# Morning Job Brief - {today}",
        "",
        f"Top {min(limit, len(jobs))} matches for {config['candidate']['name']}.",
        "",
    ]
    if packet_zip:
        lines.extend(
            [
                f"Application packet attached: `{packet_zip.name}`",
                "Each packet contains a tailored cover letter draft, resume brief, and job description snapshot.",
                "",
            ]
        )

    for index, job in enumerate(jobs[:limit], start=1):
        reason_text = ", ".join(job.reasons) if job.reasons else "keyword/profile match"
        lines.extend(
            [
                f"## {index}. {job.title}",
                f"- Score: {job.score}",
                f"- Company: {job.company}",
                f"- Location: {job.location or 'Unknown'}",
                f"- Source: {job.source}",
                f"- Posted: {job.posted or 'Unknown'}",
                f"- Why it matches: {reason_text}",
                f"- Apply: {job.url}",
                "",
                "**Job description preview**",
                "",
                truncate(clean_text(job.description), 900) or "No detailed description available from this source.",
                "",
            ]
        )

    manual_links = config.get("sources", {}).get("manual_search_links", [])
    if manual_links:
        lines.extend(["## Manual searches worth checking", ""])
        for link in manual_links:
            lines.append(f"- {link['company']}: {link['url']}")
        lines.append("")

    if warnings:
        lines.extend(["## Fetch warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")

    return "\n".join(lines)


def prepare_application_packets(jobs: list[Job], config: dict[str, Any], limit: int, out_dir: Path) -> Path | None:
    if limit <= 0:
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    packet_root = out_dir / today
    packet_root.mkdir(parents=True, exist_ok=True)
    created_files: list[Path] = []

    for index, job in enumerate(jobs[:limit], start=1):
        folder = packet_root / f"{index:02d}-{slugify(job.company)}-{slugify(job.title)}"
        folder.mkdir(parents=True, exist_ok=True)
        files = {
            "cover_letter.md": render_cover_letter(job, config),
            "tailored_resume_brief.md": render_resume_brief(job, config),
            "job_description.md": render_job_snapshot(job),
        }
        for filename, content in files.items():
            path = folder / filename
            path.write_text(content, encoding="utf-8")
            created_files.append(path)

    application = config.get("application", {})
    resume_pdf = resolve_optional_path(application.get("resume_pdf", ""))
    if resume_pdf and resume_pdf.exists():
        created_files.append(resume_pdf)

    if not created_files:
        return None

    zip_path = packet_root / f"amandeep_application_packets_{today}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in created_files:
            if packet_root in path.parents:
                archive.write(path, path.relative_to(packet_root))
            else:
                archive.write(path, f"base-documents/{path.name}")
    return zip_path


def render_cover_letter(job: Job, config: dict[str, Any]) -> str:
    candidate = config["candidate"]
    application = config.get("application", {})
    strengths = choose_strengths(job, config)
    today = datetime.now().strftime("%d %B %Y")
    company = job.company or "your team"
    title = job.title or "the role"

    return f"""# Cover Letter Draft - {company} - {title}

{today}

Dear {company} Hiring Team,

I am writing to apply for the **{title}** position. I am currently pursuing my M.Sc. in Computer Science at TU Dresden and bring three years of production software engineering experience from Nokia Networks R&D, where I worked on C/C++, Python, embedded Linux, CI/CD validation, and 4G/5G RAN software.

What makes this role especially interesting to me is the match between your needs and my recent work:

{bullet_lines(strengths)}

At Nokia, one of my strongest ownership experiences was managing OPG-layer integration work during the Marvell data-plane integration into Nokia. That work required coordinating technical dependencies, protecting quality gates, writing and owning unit tests, and debugging production issues across repository and hardware/software boundaries.

Alongside my master's coursework in software management, quality assurance, distributed systems, future-proof software systems, and large language models, I am building practical tools such as Auslander Doc Assistant, Technical PDF Translator for Students, AI-Assisted Firewall Validation Engine, and OAI RAN Metrics xApp Lab. These projects reflect how I like to work: start from a real problem, build a useful system, and make it reliable enough for others to trust.

I would be glad to discuss how my production background and current master's focus can contribute to {company}.

Best regards,

{candidate["name"]}  
{application.get("email", "amandeep.dev.eu@gmail.com")}  
{application.get("phone", "+49 174 3482786")}  
{application.get("portfolio", "https://amandesi13.github.io/amandesi-portfolio/")}  
{application.get("github", "https://github.com/amandesi13")}

## Job Link

{job.url}
"""


def render_resume_brief(job: Job, config: dict[str, Any]) -> str:
    candidate = config["candidate"]
    strengths = choose_strengths(job, config)
    matched_skills = ", ".join(keyword.strip("+") for keyword in job.reasons if keyword.startswith("+")) or "Python, C/C++, Linux, CI/CD, practical software engineering"
    return f"""# Tailored Resume Brief - {job.company} - {job.title}

Use this as the top-summary/project emphasis when tailoring the LaTeX resume for this role.

## Target Role

- Title: {job.title}
- Company: {job.company}
- Location: {job.location or "Unknown"}
- Link: {job.url}

## Recommended Profile Summary

M.Sc. Computer Science student at TU Dresden and former Nokia Networks R&D Software Engineer with three years of production experience in C/C++, Python, embedded Linux, validation automation, distributed systems debugging, and 4G/5G RAN software. Practical builder of user-facing and engineering tools including Auslander Doc Assistant, Technical PDF Translator for Students, AI-Assisted Firewall Validation Engine, and OAI RAN Metrics xApp Lab. Seeking working-student roles where production ownership, problem thinking, and reliable software delivery matter.

## Skills To Emphasize

{matched_skills}

## Best Matching Evidence

{bullet_lines(strengths)}

## Projects To Put Near The Top

- Auslander Doc Assistant: privacy-first document assistant for newcomers in Germany using FastAPI, OCR, structured extraction, and action checklists.
- Technical PDF Translator for Students: German-to-English technical PDF translator with OCR for diagrams, scans, and screenshots.
- AI-Assisted Firewall Validation Engine: Python validation engine for first-match firewall rule flow, report generation, Docker, and GitHub Actions.
- OAI RAN Metrics xApp Lab: RAN observability lab with simulated KPIs, xApp-style collection, anomaly detection, and monitoring.

## Job Description Snapshot

{truncate(clean_text(job.description), 1600) or "No detailed description available from this source."}
"""


def render_job_snapshot(job: Job) -> str:
    return f"""# Job Description Snapshot

- Company: {job.company}
- Title: {job.title}
- Location: {job.location or "Unknown"}
- Source: {job.source}
- Posted: {job.posted or "Unknown"}
- Score: {job.score}
- Link: {job.url}
- Match reasons: {", ".join(job.reasons) if job.reasons else "keyword/profile match"}

## Description

{clean_text(job.description) or "No detailed description available from this source."}
"""


def choose_strengths(job: Job, config: dict[str, Any]) -> list[str]:
    haystack = " ".join([job.title, job.company, job.description]).lower()
    strengths: list[str] = []

    if any(term in haystack for term in ["embedded", "c++", "firmware", "linux", "semiconductor"]):
        strengths.append("Production C/C++ and embedded Linux experience from Nokia Networks R&D.")
        strengths.append("Hands-on validation, debugging, and quality ownership across telecom software components.")
    if any(term in haystack for term in ["python", "automation", "ci/cd", "testing", "validation"]):
        strengths.append("Python automation and CI/CD validation experience, including test coverage and pre-deployment checks.")
    if any(term in haystack for term in ["security", "cloud", "kubernetes", "docker"]):
        strengths.append("Security and cloud tooling projects including firewall validation, Docker, GitHub Actions, and Kubernetes documentation.")
    if any(term in haystack for term in ["5g", "ran", "telecom", "network", "radio"]):
        strengths.append("4G/5G RAN background with gNB transport-layer work, packet tracing, QoS integration, and RAN metrics projects.")
    if any(term in haystack for term in ["data", "ai", "llm", "machine learning", "analytics"]):
        strengths.append("Applied AI and data experience through LLM notebook work, OCR products, and market-basket analysis.")

    strengths.append("Current M.Sc. Computer Science focus at TU Dresden in software management, quality assurance, distributed systems, and LLMs.")
    strengths.append("Product-minded GitHub projects that solve practical everyday problems, not only coursework demos.")
    return dedupe(strengths)[:5]


def send_email(markdown: str, config: dict[str, Any], attachments: list[Path]) -> None:
    email_config = config.get("email", {})
    password = os.environ.get(email_config.get("password_env", ""))
    if not password:
        raise RuntimeError("Email password env var is not set.")

    message = EmailMessage()
    message["Subject"] = email_config.get("subject", "Morning tech job matches")
    message["From"] = email_config["from"]
    message["To"] = email_config["to"]
    message.set_content(markdown)

    for path in attachments:
        if path.exists():
            message.add_attachment(
                path.read_bytes(),
                maintype="application",
                subtype="zip" if path.suffix.lower() == ".zip" else "octet-stream",
                filename=path.name,
            )

    context = ssl.create_default_context()
    with smtplib.SMTP(email_config["smtp_host"], int(email_config["smtp_port"])) as server:
        server.starttls(context=context)
        server.login(email_config["username"], password)
        server.send_message(message)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def html_to_text(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|li|div|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text)


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def format_posted(value: Any) -> str:
    if isinstance(value, (int, float)) or str(value).isdigit():
        number = int(value)
        if number > 1_000_000_000:
            return datetime.fromtimestamp(number, timezone.utc).strftime("%Y-%m-%d")
    return clean_text(value)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:70] or "job"


def bullet_lines(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def resolve_optional_path(value: str) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return Path.cwd() / path


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Collect, rank, and email Dresden working-student tech jobs.")
    parser.add_argument("--config", default="config.example.json", help="Path to config JSON.")
    parser.add_argument("--limit", type=int, default=12, help="Number of matches to include in the email/report.")
    parser.add_argument("--docs-limit", type=int, default=5, help="Number of top jobs to prepare application packets for.")
    parser.add_argument("--out-dir", default="generated_applications", help="Directory for generated application packets.")
    parser.add_argument("--no-docs", action="store_true", help="Skip cover-letter/resume packet generation.")
    parser.add_argument("--email", action="store_true", help="Send the report by email if email config is enabled.")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    jobs, warnings = collect_jobs(config)

    packet_zip = None
    if not args.no_docs:
        packet_zip = prepare_application_packets(jobs, config, args.docs_limit, Path(args.out_dir))

    markdown = render_markdown(jobs, config, warnings, args.limit, packet_zip)
    report_path = config_path.with_name("latest_job_matches.md")
    report_path.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\nSaved report: {report_path}")
    if packet_zip:
        print(f"Saved application packet: {packet_zip}")

    if args.email:
        if not config.get("email", {}).get("enabled"):
            raise RuntimeError("Email requested, but email.enabled is false in config.")
        attachments = [packet_zip] if packet_zip else []
        send_email(markdown, config, attachments)
        print("Email sent.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
