import json as json_mod
import logging

import httpx

logger = logging.getLogger(__name__)

LEETCODE_GRAPHQL_URL = "https://leetcode.com/graphql"

HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://leetcode.com",
}


def _get_auth_headers(session_cookie: str | None = None, csrftoken: str | None = None) -> dict:
    """Build headers with premium session cookie if available."""
    headers = dict(HEADERS)
    if session_cookie:
        cookies = f"LEETCODE_SESSION={session_cookie}"
        if csrftoken:
            cookies += f"; csrftoken={csrftoken}"
            headers["x-csrftoken"] = csrftoken
        headers["Cookie"] = cookies
    return headers

PROBLEM_LIST_QUERY = """
query problemsetQuestionList($skip: Int, $limit: Int) {
  problemsetQuestionList: questionList(
    categorySlug: ""
    limit: $limit
    skip: $skip
    filters: {}
  ) {
    total: totalNum
    questions: data {
      frontendQuestionId: questionFrontendId
      title
      titleSlug
      difficulty
      topicTags {
        name
      }
      acRate
      isPaidOnly
    }
  }
}
"""

USER_SOLVED_QUERY = """
query userSolvedQuestions($username: String!) {
  matchedUser(username: $username) {
    submitStatsGlobal {
      acSubmissionNum {
        difficulty
        count
      }
    }
  }
  recentAcSubmissionList(username: $username, limit: 500) {
    titleSlug
    timestamp
  }
}
"""

USER_PROGRESS_QUERY = """
query userProblemsSolved($username: String!) {
  allQuestionsCount {
    difficulty
    count
  }
  matchedUser(username: $username) {
    problemsSolvedBeatsStats {
      difficulty
      percentage
    }
    submitStatsGlobal {
      acSubmissionNum {
        difficulty
        count
      }
    }
  }
}
"""

COMPANY_PROBLEMS_QUERY = """
query getCompanyTag($slug: String!) {
  companyTag(slug: $slug) {
    name
    questions {
      questionId
      title
      titleSlug
      difficulty
      freqBar
    }
  }
}
"""

PROBLEM_DETAIL_QUERY = """
query questionContent($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    questionId
    title
    content
    difficulty
    topicTags {
      name
    }
  }
}
"""


async def fetch_problem_list(skip: int = 0, limit: int = 100) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            LEETCODE_GRAPHQL_URL,
            headers=HEADERS,
            json={"query": PROBLEM_LIST_QUERY, "variables": {"skip": skip, "limit": limit}},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"]["problemsetQuestionList"]["questions"]


async def fetch_problem_detail(slug: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            LEETCODE_GRAPHQL_URL,
            headers=HEADERS,
            json={"query": PROBLEM_DETAIL_QUERY, "variables": {"titleSlug": slug}},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"]["question"]


async def validate_username(username: str) -> bool:
    """Check if a LeetCode username actually exists."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            LEETCODE_GRAPHQL_URL,
            headers=HEADERS,
            json={
                "query": USER_PROFILE_QUERY,
                "variables": {"username": username},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"].get("matchedUser") is not None


async def fetch_user_solved(username: str) -> list[str]:
    """Fetch slugs of problems the user has solved on LeetCode."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            LEETCODE_GRAPHQL_URL,
            headers=HEADERS,
            json={"query": USER_SOLVED_QUERY, "variables": {"username": username}},
        )
        resp.raise_for_status()
        data = resp.json()
        submissions = data["data"].get("recentAcSubmissionList") or []
        # Deduplicate — LeetCode returns individual submissions, not unique problems
        seen = set()
        slugs = []
        for s in submissions:
            slug = s["titleSlug"]
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)
        return slugs


USER_PROFILE_QUERY = """
query userPublicProfile($username: String!) {
  matchedUser(username: $username) {
    submitStatsGlobal {
      acSubmissionNum {
        difficulty
        count
      }
    }
    tagProblemCounts {
      advanced {
        tagName
        problemsSolved
      }
      intermediate {
        tagName
        problemsSolved
      }
      fundamental {
        tagName
        problemsSolved
      }
    }
  }
}
"""


async def fetch_user_stats(username: str) -> dict:
    """Fetch the user's official solve counts and per-tag breakdown from LeetCode."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            LEETCODE_GRAPHQL_URL,
            headers=HEADERS,
            json={"query": USER_PROFILE_QUERY, "variables": {"username": username}},
        )
        resp.raise_for_status()
        data = resp.json()

        matched = data["data"].get("matchedUser")
        if not matched:
            return {"total": 0, "easy": 0, "medium": 0, "hard": 0, "tag_counts": {}}

        ac_list = matched.get("submitStatsGlobal", {}).get("acSubmissionNum", [])
        counts: dict = {"total": 0, "easy": 0, "medium": 0, "hard": 0}
        for item in ac_list:
            diff = item["difficulty"].lower()
            if diff == "all":
                counts["total"] = item["count"]
            elif diff in counts:
                counts[diff] = item["count"]

        # Per-tag solve counts (merge all categories)
        tag_counts: dict[str, int] = {}
        tag_data = matched.get("tagProblemCounts", {})
        for category in ("fundamental", "intermediate", "advanced"):
            for tag in tag_data.get(category, []):
                name = tag["tagName"]
                solved = tag["problemsSolved"]
                tag_counts[name] = max(tag_counts.get(name, 0), solved)

        counts["tag_counts"] = tag_counts
        return counts


async def fetch_company_problems(
    company_slug: str,
    session_cookie: str | None = None,
    csrftoken: str | None = None,
) -> list[dict]:
    """Fetch problems tagged for a specific company.

    Requires user's premium session cookie for company frequency data.
    """
    headers = _get_auth_headers(session_cookie, csrftoken)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            LEETCODE_GRAPHQL_URL,
            headers=headers,
            json={
                "query": COMPANY_PROBLEMS_QUERY,
                "variables": {"slug": company_slug},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        company_tag = data["data"].get("companyTag")
        if not company_tag:
            if not session_cookie:
                logger.warning(
                    f"Company data unavailable for '{company_slug}'. "
                    "User needs to set their LeetCode premium session cookie."
                )
            return []
        questions = company_tag.get("questions") or []
        logger.info(f"Fetched {len(questions)} problems for company '{company_slug}'")
        return questions


async def fetch_problem_company_stats(
    slug: str,
    session_cookie: str | None = None,
    csrftoken: str | None = None,
) -> dict[str, list[dict]]:
    """Fetch company interview frequency stats for a specific problem.

    Returns: {"Google": [{"times_encountered": 45, "time_range": "0-6 months"}], ...}
    Requires user's premium session cookie.
    """
    if not session_cookie:
        return {}

    query = """
    query questionCompanyStats($titleSlug: String!) {
      question(titleSlug: $titleSlug) {
        companyTagStats
      }
    }
    """
    headers = _get_auth_headers(session_cookie, csrftoken)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            LEETCODE_GRAPHQL_URL,
            headers=headers,
            json={"query": query, "variables": {"titleSlug": slug}},
        )
        resp.raise_for_status()
        data = resp.json()
        stats_raw = data.get("data", {}).get("question", {}).get("companyTagStats")
        if not stats_raw:
            return {}
        try:
            parsed = json_mod.loads(stats_raw) if isinstance(stats_raw, str) else stats_raw
            # Keys: "1" = last 6 months, "2" = 6m-1yr, "3" = 1-2 years
            result: dict[str, list[dict]] = {}
            time_labels = {"1": "0-6 months", "2": "6m-1 year", "3": "1-2 years"}
            for period, entries in parsed.items():
                for entry in entries:
                    company = entry.get("name", "Unknown")
                    if company not in result:
                        result[company] = []
                    result[company].append({
                        "times_encountered": entry.get("timesEncountered", 0),
                        "time_range": time_labels.get(period, period),
                    })
            return result
        except (json_mod.JSONDecodeError, TypeError):
            return {}


async def fetch_all_problems(max_problems: int = 3500) -> list[dict]:
    all_problems = []
    skip = 0
    limit = 100
    while skip < max_problems:
        batch = await fetch_problem_list(skip=skip, limit=limit)
        if not batch:
            break
        # skip paid-only problems
        free = [p for p in batch if not p.get("isPaidOnly")]
        all_problems.extend(free)
        skip += limit
    return all_problems
