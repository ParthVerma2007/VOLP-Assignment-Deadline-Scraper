import argparse
import asyncio
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Response, Request

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def clean_html_text(raw_html: str) -> str:
    """Strip HTML tags and unescape HTML entities to get clean text."""
    if not raw_html or not isinstance(raw_html, str):
        return ""
    text = re.sub(r'<(br|/p|/div|/li|/h\d)[^>]*>', '\n', raw_html, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def extract_course_fields(item: dict) -> dict:
    """Extract standard course information from VOLP col_list / response."""
    raw_cid = (
        item.get("crsid")
        or item.get("course_id")
        or item.get("courseId")
        or item.get("id")
    )
    raw_lid = (
        item.get("learnercoffid")
        or item.get("course_offering_learner_id")
        or item.get("courseOfferingLearnerId")
        or item.get("offeringLearnerId")
        or item.get("id")
    )

    try:
        course_id = int(raw_cid)
    except (TypeError, ValueError):
        course_id = raw_cid

    try:
        offering_learner_id = int(raw_lid)
    except (TypeError, ValueError):
        offering_learner_id = raw_lid

    course_obj = item.get("course") if isinstance(item.get("course"), dict) else {}
    course_name = (
        course_obj.get("course_name")
        or item.get("code")
        or item.get("description")
        or item.get("course_name")
        or item.get("courseName")
        or f"Course_{course_id}"
    )

    return {
        "course_id": course_id,
        "course_offering_learner_id": offering_learner_id,
        "course_name": str(course_name).strip(),
        "is_archived": item.get("is_archived", False),
        "instructor": item.get("inst")
    }


def parse_learner_course_list(json_data) -> list:
    """Parses learnerCourseList response into a normalized list of courses."""
    items = []
    if isinstance(json_data, list):
        items = json_data
    elif isinstance(json_data, dict):
        if "col_list" in json_data and isinstance(json_data["col_list"], list):
            items = json_data["col_list"]
        else:
            for key in ["courses", "course_list", "data", "result", "response", "learner_courses", "courseList"]:
                if key in json_data and isinstance(json_data[key], list):
                    items = json_data[key]
                    break
        if not items and "data" in json_data and isinstance(json_data["data"], dict):
            for subkey in ["col_list", "courses", "course_list", "list"]:
                if subkey in json_data["data"] and isinstance(json_data["data"][subkey], list):
                    items = json_data["data"][subkey]
                    break

    courses = []
    for item in items:
        if isinstance(item, dict):
            extracted = extract_course_fields(item)
            if extracted["course_id"] is not None:
                courses.append(extracted)

    return courses


def parse_assignment_item(q: dict) -> dict:
    """Extract and normalize all required assignment fields."""
    raw_question = q.get("question") or q.get("title") or q.get("name") or ""
    clean_question = clean_html_text(raw_question)

    submitted = (
        q.get("issubmitted") is True
        or q.get("submitted") is True
        or str(q.get("isalreadysubmitted", "")).lower() == "true"
        or str(q.get("isalready_submitted", "")).lower() == "true"
    )

    return {
        "ass_id": q.get("ass_id") or q.get("id"),
        "due_date": q.get("due_date") or q.get("dueDate"),
        "grace_date": q.get("grace_date"),
        "question": clean_question,
        "question_html": raw_question,
        "is_submitted": submitted,
        "weightage": q.get("weightage"),
        "is_evaluated": q.get("isevaluated", False),
        "graded": q.get("graded", False),
        "obtained_marks": q.get("obtained_marks", 0.0),
        "teacher_remark": q.get("teacher_remark") or "",
        "submitted_answer_file_name": q.get("submitted_answer_file_name"),
        "submitted_answer_file_path": q.get("submitted_answer_file_path"),
        "submitted_text_answer": q.get("submitted_text_answer"),
        "grade": q.get("grade")
    }


async def main():
    parser = argparse.ArgumentParser(description="VOLP Assignment Scraper")
    parser.add_argument("--email", default=os.getenv("VOLP_EMAIL"), help="VOLP Email")
    parser.add_argument("--password", default=os.getenv("VOLP_PASSWORD"), help="VOLP Password")
    parser.add_argument(
        "--headless",
        action="store_true",
        default=os.getenv("HEADLESS", "false").lower() == "true",
        help="Run browser in headless mode"
    )
    parser.add_argument(
        "--output",
        default=str(BASE_DIR / "volp_data.json"),
        help="Output JSON file path"
    )
    args = parser.parse_args()

    email = args.email
    password = args.password

    if not email or not password:
        print("[!] Error: VOLP_EMAIL and VOLP_PASSWORD must be provided via environment variables, .env, or CLI flags.")
        print("    Example: python scraper.py --email user@example.com --password mysecretpass")
        sys.exit(1)

    print("=" * 70)
    print("                VOLP ASSIGNMENT & DUE DATE SCRAPER                ")
    print("=" * 70)
    print(f"[*] Account: {email}")
    print(f"[*] Headless mode: {args.headless}")
    print(f"[*] Output destination: {args.output}\n")

    captured_course_list = None
    captured_headers = {}
    captured_token = None
    course_list_event = asyncio.Event()

    async with async_playwright() as p:
        print("[1/5] Launching Chromium browser...")
        browser = await p.chromium.launch(
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Intercept requests to capture auth Token dynamically on every login
        async def handle_request(request: Request):
            nonlocal captured_token, captured_headers
            url = request.url
            if "learner.volp.in" in url:
                req_headers = await request.all_headers()
                for key, val in req_headers.items():
                    if key.lower() == "token" and val:
                        captured_token = val
                        captured_headers["Token"] = val
                    elif key.lower() == "device" and val:
                        captured_headers["Device"] = val

        # Intercept responses to capture learnerCourseList JSON body
        async def handle_response(response: Response):
            nonlocal captured_course_list
            url = response.url
            if "learnerCourseList" in url and response.request.method == "POST":
                if response.status == 200:
                    try:
                        text = await response.text()
                        if text:
                            data = json.loads(text)
                            captured_course_list = data
                            print(f"    [+] Captured learnerCourseList ({len(text)} bytes)")
                            course_list_event.set()
                    except Exception as e:
                        print(f"    [!] Error reading learnerCourseList response body: {e}")

        page.on("request", handle_request)
        page.on("response", handle_response)

        # 2. Navigate and Log in
        print("[2/5] Navigating to VOLP login page (https://classroom.volp.in/login)...")
        await page.goto("https://classroom.volp.in/login", wait_until="networkidle", timeout=60000)

        email_selector = 'input[type="email"], input[name="email"], input[name="username"], input[placeholder*="email" i], input[placeholder*="user" i], #email, #username'
        password_selector = 'input[type="password"], input[name="password"], #password'
        submit_selector = 'button[type="submit"], input[type="submit"], button:has-text("Login"), button:has-text("Sign In"), button:has-text("Log In")'

        if await page.locator(email_selector).first.is_visible():
            print("[3/5] Entering login credentials...")
            await page.locator(email_selector).first.fill(email)
            await page.locator(password_selector).first.fill(password)
            await page.locator(submit_selector).first.click()

            print("    Waiting for dashboard navigation...")
            try:
                await page.wait_for_url(lambda u: "login" not in u.lower(), timeout=30000)
                print(f"    [+] Login successful! Redirected to: {page.url}")
            except Exception:
                await page.wait_for_load_state("networkidle")
                print(f"    [+] Current page after login attempt: {page.url}")
        else:
            print("[3/5] Already authenticated.")

        # Ensure we land on My Courses to fire the event
        await page.wait_for_load_state("networkidle")
        if "my-courses" not in page.url:
            print("    [*] Navigating to My Courses (/learner/my-courses)...")
            try:
                my_courses_btn = page.locator('a[href*="my-courses"], button:has-text("My Courses"), span:has-text("My Courses")').first
                if await my_courses_btn.is_visible():
                    await my_courses_btn.click()
                else:
                    await page.goto("https://classroom.volp.in/learner/my-courses", wait_until="networkidle")
            except Exception as e:
                print(f"    [*] Navigation note: {e}")

        # 3. Wait for learnerCourseList response
        print("[4/5] Retrieving course list...")
        try:
            await asyncio.wait_for(course_list_event.wait(), timeout=12.0)
        except asyncio.TimeoutError:
            print("    [*] Checking captured tokens and local state...")

        # Also extract token from browser storage if needed
        storage_dump = await page.evaluate("""() => {
            let res = {};
            for (let i = 0; i < localStorage.length; i++) {
                let k = localStorage.key(i);
                res[k] = localStorage.getItem(k);
            }
            for (let i = 0; i < sessionStorage.length; i++) {
                let k = sessionStorage.key(i);
                res['session_' + k] = sessionStorage.getItem(k);
            }
            return res;
        }""")

        for k, v in storage_dump.items():
            if isinstance(v, str) and ("token" in k.lower() or len(v) > 50):
                if not captured_token:
                    captured_token = v
                    captured_headers["Token"] = v
                    print(f"    [+] Retrieved Token from storage key: {k}")

        courses = []
        if captured_course_list:
            courses = parse_learner_course_list(captured_course_list)

        print(f"\n[+] Total courses discovered: {len(courses)}")
        for idx, c in enumerate(courses, 1):
            archived = " [Archived]" if c.get("is_archived") else ""
            print(f"    {idx}. {c['course_name']}{archived} (Course ID: {c['course_id']}, Offering Learner ID: {c['course_offering_learner_id']})")

        # 4. Fetch assignments and due dates for each course
        print("\n[5/5] Retrieving subjective assignments and due dates for each course...")
        print(f"    Auth Token: {'Dynamic Token Captured (len=' + str(len(captured_token)) + ')' if captured_token else 'NOT FOUND'}")
        
        all_course_data = []
        assignment_url = "https://learner.volp.in/SubjectiveAssignment/getSubjectiveAssignment_new"

        for idx, c in enumerate(courses, 1):
            cid = c["course_id"]
            lid = c["course_offering_learner_id"]
            name = c["course_name"]

            print(f"\n    [{idx}/{len(courses)}] Checking: {name}")
            print(f"        Course ID: {cid} | Offering Learner ID: {lid}")

            eval_fetch_code = """
            async (args) => {
                const { lid, cid, token, url } = args;

                const res = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'Accept': 'application/json, text/plain, */*',
                        'Content-Type': 'application/json;charset=UTF-8',
                        'Device': 'Web',
                        'Router-Path': '/learner-subjective-assignment',
                        'Token': token
                    },
                    body: JSON.stringify({
                        course_offering_learner_id: lid,
                        type: 'content',
                        courseId: cid
                    })
                });

                const status = res.status;
                const text = await res.text();
                return { status: status, text: text };
            }
            """

            assignments = []
            try:
                res = await page.evaluate(eval_fetch_code, {
                    "lid": lid,
                    "cid": cid,
                    "token": captured_token or "",
                    "url": assignment_url
                })

                status = res.get("status")
                raw_text = res.get("text", "")
                # print("\n========== SUBJECTIVE API DEBUG ==========")
                # print("HTTP STATUS:", status)
                # print("RAW RESPONSE:")
                # print(raw_text[:5000])
                # print("==========================================\n")
                
                if status == 200:
                    try:
                        res_json = json.loads(raw_text)
                        raw_questions = []
                        if isinstance(res_json, dict):
                            raw_questions = res_json.get("question_list") or res_json.get("data") or []
                        elif isinstance(res_json, list):
                            raw_questions = res_json

                        for q in raw_questions:
                            if isinstance(q, dict):
                                assignments.append(parse_assignment_item(q))
                    except Exception as e:
                        print(f"        [!] JSON parsing error: {e} | Raw text: {raw_text[:120]}")
                else:
                    print(f"        [!] Server returned status {status}: {raw_text[:200]}")

            except Exception as e:
                print(f"        [!] Exception during fetch: {e}")

            if assignments:
                print(f"        [+] Retrieved {len(assignments)} assignment(s):")
                for a_idx, a in enumerate(assignments, 1):
                    sub_status = "[SUBMITTED]" if a["is_submitted"] else "[PENDING]"
                    q_preview = a["question"].replace("\n", " ")[:60]
                    print(f"            {a_idx}. {sub_status} Due: {a['due_date']} | Marks: {a['weightage']} | {q_preview}...")
            else:
                print(f"        [-] 0 subjective assignments.")

            all_course_data.append({
                "course_id": cid,
                "course_offering_learner_id": lid,
                "course_name": name,
                "instructor": c.get("instructor"),
                "is_archived": c.get("is_archived", False),
                "assignments_count": len(assignments),
                "assignments": assignments
            })

        # 5. Save structured output to JSON
        final_output = {
            "retrieved_at": datetime.now().isoformat(),
            "total_courses": len(all_course_data),
            "courses": all_course_data
        }

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(final_output, f, indent=4, ensure_ascii=False)

        print("\n" + "=" * 70)
        print(f"[SUCCESS] All assignments extracted and saved to: {output_path.resolve()}")
        print("=" * 70)

        if not args.headless:
            await asyncio.sleep(2)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
