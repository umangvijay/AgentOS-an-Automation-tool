import json
from typing import Optional
from google.adk.agents.llm_agent import LlmAgent
from backend.services.llm_context import adk_gemini
from backend.repositories.memory_repository import MemoryRepository
from backend.services.embedding_service import EmbeddingService
from backend.models.resume import Resume
from backend.services.jd_parser_service import JDParserService
from backend.services.resume_tailor_service import ResumeTailorService

def get_orchestrator_agent(tool_router=None, catalog_json: str = "[]", memory_repo: MemoryRepository = None, embedding_service: EmbeddingService = None, user_id: str = "default_user", workflow_context: str = "", execution_context: dict = None, model: Optional[str] = None) -> LlmAgent:
    llm = adk_gemini(model, tools=[{"google_search": {}}])
    

    async def _progress(text: str) -> None:
        emit = (execution_context or {}).get("emit_progress")
        if emit:
            try:
                await emit(text)
            except Exception:
                pass

    async def call_external_tool(agent_tool_name: str, arguments_json: str) -> str:
        """Call a tool from the external tool catalog.
        Args:
            agent_tool_name: The agent_tool_name from the catalog (e.g. mcp1__add).
            arguments_json: JSON string containing the arguments according to the tool's input_schema.
        """
        if not tool_router:
            return "Error: ToolRouter not initialized."
        await _progress(f"Calling tool {agent_tool_name}")
        try:
            args = json.loads(arguments_json)
            ctx = {"user_id": user_id, **(execution_context or {})}
            result = await tool_router.execute_tool_safe(agent_tool_name, args, context=ctx)
            return json.dumps(result) if isinstance(result, (dict, list)) else str(result)
        except Exception as e:
            # To bubble up retries/failures to the workflow engine, we MUST raise it!
            raise e

    async def build_integration(api_docs_url_or_description: str, name: str, api_credential_name: str = "") -> str:
        """Build a NEW integration when no tool in the catalog covers the application you need.
        The MCP factory ingests the API docs/OpenAPI spec at the URL (or the plain-text
        description), generates tool schemas, live-tests them, and registers the tools.
        Args:
            api_docs_url_or_description: URL to an OpenAPI spec / API docs page, or a natural-language API description.
            name: Human-readable integration name (the application or API).
            api_credential_name: Optional name of a stored credential (holding an api_key/token)
                to attach so the new tools authenticate to the API (e.g. "stripe").
        Returns: JSON with the build result and the refreshed tool catalog.
        """
        if not tool_router:
            return "Error: ToolRouter not initialized."
        await _progress(f"Building MCP integration: {name or api_docs_url_or_description[:60]}")
        try:
            from backend.agents.mcp_factory.mcp_factory_agent import MCPFactoryAgent
            factory_agent = MCPFactoryAgent(
                tool_router.mcp_repo,
                secrets_repo=getattr(tool_router, "secrets_repo", None),
            )
            source = api_docs_url_or_description.strip()
            from backend.mcp.website_mcp import looks_like_website_without_api
            if looks_like_website_without_api(
                source, url=source if source.startswith(("http://", "https://")) else None
            ):
                method = "website"
            elif source.startswith(("http://", "https://")):
                method = "url"
            else:
                method = "prompt"
            result = await factory_agent.run_build(
                user_id=user_id, method=method, source=source, name=name,
            )
            if result.get("status") == "success" and api_credential_name:
                existing_auth = {}
                try:
                    mcp_row = await tool_router.mcp_repo.get_mcp(result["mcp_id"])
                    raw_auth = (mcp_row or {}).get("auth") or {}
                    if isinstance(raw_auth, str):
                        import json as _json
                        raw_auth = _json.loads(raw_auth)
                    if isinstance(raw_auth, dict):
                        existing_auth = raw_auth
                except Exception:
                    existing_auth = {}
                new_auth = {"type": "API_KEY", "credential_ref": f"cred:{api_credential_name}"}
                placement = {k: existing_auth[k] for k in ("in", "name") if k in existing_auth}
                merged = {**placement, **new_auth}
                await tool_router.mcp_repo.update_mcp_auth(result["mcp_id"], merged)
                result["credential_attached"] = api_credential_name
            fresh_catalog = await tool_router.get_tool_catalog(user_id)
            return json.dumps({"build": result, "catalog": fresh_catalog})
        except Exception as e:
            return json.dumps({"build": {"status": "error", "message": str(e)}})

    async def browse_website(goal: str, start_url: str, credential_name: str = "") -> str:
        """Perform human-like work in a real web browser on the user's behalf:
        log in to a website, click buttons, fill forms, complete exercises/tasks,
        and extract information.
        Args:
            goal: Precise description of what to accomplish on the site
                (e.g. "Log in, open today's exercises, and complete each one").
            start_url: The URL to open first (e.g. "https://example.com/login").
            credential_name: Optional name of a stored credential with the site login
                (fields like username/password). Ask the user to store it via the
                Credentials page if it does not exist.
        Returns: JSON with success flag, a result summary, and the audited step list.
        """
        await _progress(f"Opening a real browser at {start_url}")
        try:
            from backend.services.web_agent import WebAgent
            agent = WebAgent(secrets_repo=getattr(tool_router, "secrets_repo", None))
            outcome = await agent.run(
                goal=goal,
                start_url=start_url,
                user_id=user_id,
                credential_name=credential_name or None,
                max_steps=50,
            )
            return json.dumps(outcome)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})

    async def send_email(to: str, subject: str, body: str, html: bool = False) -> str:
        """Send a real email on the user's behalf via their configured SMTP account.
        Requires a stored credential named "smtp" (fields: host, port, username, password).
        Args:
            to: Recipient email address (comma-separate for multiple recipients).
            subject: Email subject line.
            body: Email body. Write it fully and professionally — you are writing as the user.
            html: Set true if body is HTML.
        Returns: JSON confirmation or an error explaining what is missing.
        """
        await _progress(f"Sending email to {to}")
        try:
            from backend.services import email_service
            recipients = [addr.strip() for addr in to.split(",") if addr.strip()]
            result = await email_service.send_email(
                getattr(tool_router, "secrets_repo", None),
                user_id, recipients, subject, body, html=html,
            )
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"sent": False, "error": str(e)})

    async def list_stored_credentials() -> str:
        """List the names of credentials the user has stored (site logins, smtp, API keys).
        Values are never revealed. Use this to check whether a login or API key exists
        before calling browse_website, send_email, or build_integration."""
        secrets_repo = getattr(tool_router, "secrets_repo", None)
        if not secrets_repo:
            return json.dumps({"credentials": []})
        try:
            keys = await secrets_repo.list_secret_keys(user_id)
            names = [k[5:] for k in keys if k.startswith("cred:")]
            return json.dumps({"credentials": names})
        except Exception as e:
            return json.dumps({"credentials": [], "error": str(e)})

    async def search_memory(query: str, limit: int = 5) -> str:
        """Search the semantic memory for relevant past context.
        Args:
            query: The search query string.
            limit: Maximum number of results to return.
        """
        if not memory_repo or not embedding_service:
            return "Error: Memory system not initialized."
        try:
            embedding = embedding_service.embed_text(query)
            results = await memory_repo.search_memory(user_id, embedding, limit=limit)
            if not results:
                return "No relevant memory found."
            return json.dumps([
                {"id": r["id"], "content": r["content"], "score": r.get("similarity_score", 0.0)}
                for r in results
            ])
        except Exception as e:
            return f"Error searching memory: {str(e)}"

    async def store_memory(content: str, metadata_json: str = "{}") -> str:
        """Store semantic context into memory for future retrieval.
        Args:
            content: The text content to remember.
            metadata_json: JSON string of structured metadata.
        """
        if not memory_repo or not embedding_service:
            return "Error: Memory system not initialized."
        try:
            metadata = json.loads(metadata_json)
            embedding = embedding_service.embed_text(content)
            doc_id = await memory_repo.store_memory(user_id, content, "semantic", metadata, embedding)
            return f"Memory stored successfully with ID: {doc_id}"
        except Exception as e:
            return f"Error storing memory: {str(e)}"

    async def analyze_resume_ats(resume_json: str, jd_text: str) -> str:
        """Score a structured resume JSON against a job description for ATS match."""
        try:
            from backend.services.resume_builder import score_resume
            return json.dumps(score_resume(resume_json, jd_text))
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def tailor_resume(resume_json: str, jd_text: str, target_job_id: str) -> str:
        """Tailor a master resume to a job description without fabricating facts."""
        try:
            resume = Resume.model_validate_json(resume_json)
            jd = JDParserService().parse_job_description(jd_text)
            tailored = ResumeTailorService().tailor(resume, jd, target_job_id)
            return tailored.model_dump_json()
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def create_resume(profile_text: str, job_description: str = "", tailor: bool = False) -> str:
        """Create an ATS-ready resume from free-form background text (notes, LinkedIn dump, old CV).
        Optionally score and tailor it against a job description.
        Args:
            profile_text: Everything known about the candidate. Do not invent employers or metrics.
            job_description: Optional JD to score (and optionally tailor) against.
            tailor: If true and a JD is provided, rewrite bullets toward the JD without fabricating facts.
        """
        try:
            from backend.services.resume_builder import create_and_score
            result = await create_and_score(profile_text, job_description, tailor=tailor)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def fetch_webpage(url: str, extract: str = "text") -> str:
        """Fetch a public web page or API endpoint and return its content so you can
        read documentation, APIs, GitHub repos, blog posts, or any site directly.
        Use this to jump between websites while working on a task: read a docs page,
        follow a link, check an API response, or pull data from a site.
        Args:
            url: Absolute https URL of the page or endpoint.
            extract: "text" for readable page text, "html" for raw HTML, "json" for API responses.
        Returns: JSON with status, final URL, and the page content (truncated to ~15k chars).
        """
        await _progress(f"Reading {url}")
        try:
            import httpx
            from backend.mcp.builder.openapi_parser import OpenAPIParser
            parser = OpenAPIParser()
            parser._validate_ssrf(url)
            headers = {"User-Agent": "Mozilla/5.0 (compatible; AgentOS/2.0; +https://agentos.app)"}
            url_hop = url
            for _hop in range(4):
                async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
                    resp = await client.get(url_hop, headers=headers)
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location", "")
                    if not loc:
                        break
                    url_hop = str(httpx.URL(url_hop).join(loc))
                    parser._validate_ssrf(url_hop)
                    continue
                break
            if (resp.status_code // 100) != 2:
                return json.dumps({"ok": False, "status": resp.status_code, "url": url_hop,
                                   "error": f"Upstream returned HTTP {resp.status_code}"})
            content_type = resp.headers.get("content-type", "")
            if extract == "html" or "json" in content_type or extract == "json":
                body = resp.text[:15000]
            else:
                # Readable-text extraction: strip tags/scripts/styles.
                import re as _re
                html = resp.text
                html = _re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
                html = _re.sub(r"(?s)<[^>]+>", " ", html)
                import html as _html
                body = _re.sub(r"\s+", " ", _html.unescape(html)).strip()[:15000]
            return json.dumps({"ok": True, "status": resp.status_code, "url": url_hop,
                               "content_type": content_type, "content": body})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    async def web_research(goal: str, max_steps: int = 6) -> str:
        """Multi-hop web research: search Google, open the most relevant pages, and
        follow leads across different websites until the goal is answered with sources.
        Use for questions that need current information from multiple sites — docs,
        GitHub issues, release notes, pricing pages, comparisons.
        Args:
            goal: What to find out. Be specific about the facts or links needed.
            max_steps: Max research hops (default 6, hard cap 10).
        Returns: JSON with a synthesized answer and the list of source URLs visited.
        """
        await _progress(f"Researching the web: {goal[:80]}")
        try:
            from backend.agents.research.research_agent import ResearchAgent
            agent = ResearchAgent()
            outcome = await agent.research(goal, max_steps=max(1, min(int(max_steps or 6), 10)))
            return json.dumps(outcome)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    async def scrape_website(url: str, want: str = "overview") -> str:
        """Scrape structured data from any public website: title, meta description,
        headings, links, tables, emails/phones, and page text. Works without an API —
        use it for data extraction, lead lists, research, or checking a page's content.
        For JavaScript-only sites, use browse_website instead.
        Args:
            url: Absolute https URL of the page to scrape.
            want: "overview" (title/description/headings/links), "text" (full readable text),
                  "links" (every link with anchor), "tables" (all tables as rows),
                  or "contacts" (emails and phone numbers found on the page).
        Returns: JSON with the extracted structured data.
        """
        await _progress(f"Scraping {url}")
        try:
            import httpx
            import re as _re
            import html as _html
            from backend.mcp.builder.openapi_parser import OpenAPIParser
            parser = OpenAPIParser()
            parser._validate_ssrf(url)
            headers = {"User-Agent": "Mozilla/5.0 (compatible; AgentOS/2.0; +https://agentos.app)"}
            url_hop = url
            body = ""
            for _hop in range(4):
                async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
                    resp = await client.get(url_hop, headers=headers)
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    url_hop = str(httpx.URL(url_hop).join(resp.headers["Location"]))
                    parser._validate_ssrf(url_hop)
                    continue
                body = resp.text if resp.status_code // 100 == 2 else ""
                break
            if not body:
                return json.dumps({"ok": False, "error": f"Could not fetch page (HTTP {resp.status_code})"})
            title = (match := _re.search(r"(?is)<title[^>]*>(.*?)</title>", body)) and _re.sub(r"\s+", " ", match.group(1)).strip()
            desc = (match := _re.search(r"(?is)<meta[^>]+name=[\"']description[\"'][^>]+content=[\"'](.*?)[\"']", body)) and match.group(1)[:400]
            heads = [{"level": int(m.group(1)), "text": _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", "", m.group(2))).strip()[:160]}
                     for m in _re.finditer(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>", body)]
            links = [{"text": _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", "", m.group(1))).strip()[:120] or "(no text)",
                      "href": m.group(2)[:300]}
                     for m in _re.finditer(r"(?is)<a[^>]+href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", body)]
            seen = set(); uniq_links = [l for l in links if not (l["href"] in seen or seen.add(l["href"]))][:200]
            text = _re.sub(r"\s+", " ", _html.unescape(_re.sub(r"(?s)<[^>]+>", " ", _re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", body)))).strip()
            tables = []
            for tm in _re.finditer(r"(?is)<table[^>]*>(.*?)</table>", body):
                rows = []
                for rm in _re.finditer(r"(?is)<tr[^>]*>(.*?)</tr>", tm.group(1)):
                    cells = [_re.sub(r"\s+", " ", _html.unescape(_re.sub(r"(?s)<[^>]+>", "", cm.group(1)))).strip()
                             for cm in _re.finditer(r"(?is)<t[hd][^>]*>(.*?)</t[hd]>", rm.group(1))]
                    if any(cells):
                        rows.append(cells)
                if rows:
                    tables.append(rows[:60])
            contacts = {
                "emails": sorted(set(_re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)))[:50],
                "phones": sorted(set(_re.findall(r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3,4}[\s.-]\d{3,4}(?:[\s.-]\d{3,4})?", text)))[:30],
            }
            want = (want or "overview").lower()
            data = {"ok": True, "url": url_hop, "title": title, "description": desc}
            if want in ("overview", "links"):
                data["headings"] = heads[:80]
                data["links"] = uniq_links
            if want in ("overview", "text"):
                data["text"] = text[:15000]
            if want in ("overview", "tables"):
                data["tables"] = tables[:10]
            if want in ("overview", "contacts"):
                data.update(contacts)
            return json.dumps(data)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    async def create_files(name: str, files_json: str) -> str:
        """Create real downloadable files (reports, CSVs, code, docs, HTML) from your work.
        The user gets permanent download links. Call this whenever the task produces a
        document, dataset, or code the user should keep — do not just print content in chat.
        Args:
            name: Short artifact name, e.g. "competitor-report" or "leads-csv".
            files_json: JSON array like [{"path": "report.md", "content": "full file text"}].
        Returns: JSON with the artifact id and download URLs for every file.
        """
        await _progress(f"Creating downloadable files: {name}")
        try:
            from backend.services.artifact_builder import save_user_files, ArtifactError
            files = json.loads(files_json)
            if not isinstance(files, list) or not files:
                return json.dumps({"ok": False, "error": "files_json must be a non-empty array of {path, content}"})
            meta = save_user_files(user_id, name, files[:40])
            base = f"/api/v1/artifacts/{meta['artifact_id']}/files"
            return json.dumps({
                "ok": True,
                "artifact_id": meta["artifact_id"],
                "download_urls": {f["path"]: f"{base}/{f['path']}" for f in meta["files"]},
            })
        except ArtifactError as e:
            return json.dumps({"ok": False, "error": f"Rejected: {e}"})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    async def check_website_health(url: str) -> str:
        """Live health check of any public website or HTTP API: DNS, TLS, latency, status, redirects, security headers.
        Args:
            url: Absolute http(s) URL of the site or endpoint to probe.
        """
        try:
            from backend.services.website_health import check_website
            return json.dumps(await check_website(url))
        except Exception as e:
            return json.dumps({"error": str(e), "grade": "down"})

    async def debug_code(source: str, language: str = "python", error_message: str = "", goal: str = "") -> str:
        """Diagnose bugs in source code. Runs a syntax check (Python) plus an LLM diagnosis.
        Does not execute the submitted code.
        Args:
            source: Full source to debug.
            language: Language id (python, javascript, typescript, go, ...).
            error_message: Optional stack trace or error text.
            goal: Optional description of expected behavior.
        """
        try:
            from backend.services.code_debugger import debug_code as _debug
            return json.dumps(await _debug(source, language, error_message, goal))
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def generate_project(brief: str, kind: str = "website", name: str = "", scale: str = "") -> str:
        """Generate a real website or software project as downloadable files.
        Use kind="website" for a static site. Use kind="app" for a local project with README.
        scale: compact (landing), standard (medium), full (large multi-page/app).
        Args:
            brief: What to build, audience, pages/features, look and feel.
            kind: "website" or "app".
            name: Optional short project name.
            scale: compact | standard | full. Infer from the brief if empty.
        """
        try:
            from backend.services.artifact_builder import generate_project as _gen
            lowered = (brief or "").lower()
            chosen = (scale or "").lower().strip()
            if chosen not in ("compact", "standard", "full"):
                if any(w in lowered for w in ("landing", "one page", "single page", "compact")):
                    chosen = "compact"
                elif any(w in lowered for w in ("full app", "large", "production", "multi-page", "complete app", "medium")):
                    chosen = "full"
                else:
                    chosen = "standard"
            return json.dumps(await _gen(user_id, brief, kind=kind, name=name, scale=chosen))
        except Exception as e:
            return json.dumps({"error": str(e)})

    instruction = f"""You are the Orchestrator Agent. Execute the given task using the best available capability.

External tool catalog (APIs already connected for this user):
{catalog_json}

How to work:
- CHAIN TOOLS. Do not stop after one step or one source. Typical chains:
  research a topic (web_research) → open the best pages (fetch_webpage / scrape_website) →
  build a missing API connector (build_integration) → call it (call_external_tool) →
  save the outcome as downloadable files (create_files) or email it (send_email).
  If one tool's result is thin, jump to another source or tool — GitHub, docs sites, blogs,
  the target API itself — until you have a solid answer. Cross-check important facts across sources.
- DELIVERABLES: whenever the task produces a report, list, dataset, or code, call create_files
  so the user gets download links. Never paste a large file into chat instead of saving it.
- SHOW YOUR WORK: narrate briefly what you are doing and cite URLs for web-sourced claims.
- For ANY application/API (payments, CRM, email providers, issue trackers, cloud, internal tools, etc.):
  if a matching tool is in the catalog, call `call_external_tool` with its agent_tool_name and JSON arguments.
  If it is missing, call `build_integration` with the app's OpenAPI URL, API docs URL, a website URL, or a precise description.
  Websites with no OpenAPI still get catalog tools (browser tools). HTTP APIs get REST tools. Then use them.
  Pass `api_credential_name` when the user has stored an API key (check `list_stored_credentials` first).
- For work inside a website: prefer catalog tools for that site (`call_external_tool`), or `browse_website`.
  Pass a stored credential name for logins. Never ask the user to paste passwords into chat.
- Cross-site work: `fetch_webpage` reads any docs page, GitHub repo/file, API endpoint, or article;
  `web_research` runs multi-hop search+read across sites with citations. Use them freely — jump between
  sources, follow links, and verify facts before answering. `check_website_health` probes uptime/TLS.
- Build a landing page or a medium/large website or app as files: `generate_project` (use scale full for multi-page/apps).
- Email is one outbound action: `send_email` (needs a stored "smtp" credential). Prefer an app's own API
  via build_integration + call_external_tool when that is what the user asked for. Do not stop at drafting mail
  when the user asked to browse a site, build an app, or call APIs.
- Resumes: `create_resume` from free text; `analyze_resume_ats` / `tailor_resume` when structured JSON already exists.
- Long-term context: `search_memory` / `store_memory`.
Do not guess private URLs. If a tool fails, the workflow engine handles retries.

Workflow History Context:
{workflow_context}
"""
    
    agent_tools = [
        call_external_tool, build_integration, browse_website, send_email,
        list_stored_credentials, check_website_health, debug_code, generate_project,
        create_resume, analyze_resume_ats, tailor_resume,
        fetch_webpage, web_research, scrape_website, create_files,
    ]
    if memory_repo and embedding_service:
        agent_tools.extend([search_memory, store_memory])
        
    return LlmAgent(
        name="OrchestratorAgent",
        instruction=instruction,
        model=llm,
        tools=agent_tools
    )
