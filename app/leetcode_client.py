import httpx

LEETCODE_GRAPHQL_URL = "https://leetcode.com/graphql"

HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://leetcode.com",
}

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


async def fetch_company_problems(company_slug: str) -> list[dict]:
    """Fetch problems tagged for a specific company (e.g. 'google', 'meta', 'amazon')."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            LEETCODE_GRAPHQL_URL,
            headers=HEADERS,
            json={
                "query": COMPANY_PROBLEMS_QUERY,
                "variables": {"slug": company_slug},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        company_tag = data["data"].get("companyTag")
        if not company_tag:
            return []
        return company_tag.get("questions") or []


async def fetch_all_problems(max_problems: int = 500) -> list[dict]:
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
