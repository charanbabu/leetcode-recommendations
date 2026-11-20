import json
import random
from collections import Counter

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompanyProblem, Problem, Submission, User, Difficulty, SubmissionStatus


DIFFICULTY_ORDER = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]

# Round-robin topic rotation order
TOPIC_ORDER = [
    "Array", "String", "Hash Table", "Two Pointers", "Sliding Window",
    "Stack", "Queue", "Linked List",
    "Binary Search", "Sorting",
    "Tree", "Binary Tree", "Binary Search Tree",
    "Graph", "BFS", "DFS",
    "Dynamic Programming", "Greedy",
    "Backtracking", "Recursion",
    "Heap (Priority Queue)", "Trie",
    "Bit Manipulation", "Math",
]


async def get_recommended_problem(db: AsyncSession, user: User) -> Problem | None:
    # Gather user history
    subs_result = await db.execute(
        select(Submission).where(Submission.user_id == user.id)
    )
    submissions = subs_result.scalars().all()

    sent_ids = {s.problem_id for s in submissions}
    solved_ids = {s.problem_id for s in submissions if s.status == SubmissionStatus.SOLVED}

    # Count solved per difficulty
    solved_problems_result = await db.execute(
        select(Problem).where(Problem.id.in_(solved_ids))
    ) if solved_ids else None
    solved_problems = solved_problems_result.scalars().all() if solved_problems_result else []

    solved_counts: dict[Difficulty, int] = {d: 0 for d in Difficulty}
    solved_tags: list[str] = []
    for p in solved_problems:
        solved_counts[p.difficulty] += 1
        if p.tags:
            solved_tags.extend(t.strip() for t in p.tags.split(","))

    solved_tag_counts = Counter(solved_tags)

    # Weak topics — skipped/attempted/unsolved
    weak_problem_ids = {s.problem_id for s in submissions
                        if s.status in (SubmissionStatus.SENT, SubmissionStatus.ATTEMPTED, SubmissionStatus.SKIPPED)}
    weak_result = await db.execute(
        select(Problem).where(Problem.id.in_(weak_problem_ids))
    ) if weak_problem_ids else None
    weak_problems = weak_result.scalars().all() if weak_result else []

    weak_tags: list[str] = []
    for p in weak_problems:
        if p.tags:
            weak_tags.extend(t.strip() for t in p.tags.split(","))
    weak_tag_counts = Counter(weak_tags)

    target_difficulty = _pick_difficulty(solved_counts)

    # Load company data
    company_problem_freq: dict[int, float] = {}
    if user.target_company:
        cp_result = await db.execute(
            select(CompanyProblem).where(CompanyProblem.company == user.target_company.lower())
        )
        for cp in cp_result.scalars().all():
            company_problem_freq[cp.problem_id] = cp.frequency

    # --- Round-robin topic selection ---
    next_topic, next_topic_index = _next_topic(user.last_topic_index, solved_tag_counts)

    # Try to find a problem in the next topic (try up to full rotation)
    problem = None
    tried = 0
    current_index = next_topic_index

    while tried < len(TOPIC_ORDER):
        topic = TOPIC_ORDER[current_index]
        problem = await _pick_best_in_topic(
            db, sent_ids, topic, target_difficulty,
            solved_tag_counts, weak_tag_counts, company_problem_freq,
        )
        if problem:
            # Update user's topic pointer
            user.last_topic_index = current_index
            break
        # No unsent problems in this topic, skip to next
        current_index = (current_index + 1) % len(TOPIC_ORDER)
        tried += 1

    if not problem:
        # All topics exhausted — fall back to best scored from any topic
        problem = await _pick_any_best(
            db, sent_ids, target_difficulty,
            solved_tag_counts, weak_tag_counts, company_problem_freq,
        )

    if not problem:
        problem = await _pick_unsolved_problem(db, user.id)

    return problem


def _next_topic(last_index: int, solved_tag_counts: Counter) -> tuple[str, int]:
    """Advance to the next topic in rotation."""
    next_index = (last_index + 1) % len(TOPIC_ORDER)
    return TOPIC_ORDER[next_index], next_index


async def _pick_best_in_topic(
    db: AsyncSession,
    sent_ids: set[int],
    topic: str,
    target_difficulty: Difficulty,
    solved_tag_counts: Counter,
    weak_tag_counts: Counter,
    company_freq: dict[int, float],
) -> Problem | None:
    """Pick the best unsent problem in a specific topic."""
    ok_difficulties = _acceptable_difficulties(target_difficulty)

    # Find unsent problems that have this topic in their tags
    query = select(Problem).where(
        Problem.tags.like(f"%{topic}%"),
        Problem.difficulty.in_(ok_difficulties),
    )
    if sent_ids:
        query = query.where(Problem.id.not_in(sent_ids))

    result = await db.execute(query)
    candidates = result.scalars().all()

    if not candidates:
        return None

    # Score and pick best
    scored = []
    for p in candidates:
        score = _score_problem(p, target_difficulty, solved_tag_counts, weak_tag_counts, company_freq.get(p.id))
        scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Weighted pick from top 3 to add slight variety within topic
    top_n = scored[:3]
    weights = [s[0] + 1 for s in top_n]
    chosen = random.choices(top_n, weights=weights, k=1)[0]
    return chosen[1]


async def _pick_any_best(
    db: AsyncSession,
    sent_ids: set[int],
    target_difficulty: Difficulty,
    solved_tag_counts: Counter,
    weak_tag_counts: Counter,
    company_freq: dict[int, float],
) -> Problem | None:
    """Fallback: pick best from any unsent problem."""
    query = select(Problem)
    if sent_ids:
        query = query.where(Problem.id.not_in(sent_ids))

    result = await db.execute(query)
    candidates = result.scalars().all()

    if not candidates:
        return None

    scored = []
    for p in candidates:
        score = _score_problem(p, target_difficulty, solved_tag_counts, weak_tag_counts, company_freq.get(p.id))
        scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_n = scored[:5]
    weights = [s[0] + 1 for s in top_n]
    chosen = random.choices(top_n, weights=weights, k=1)[0]
    return chosen[1]


def _score_problem(
    problem: Problem,
    target_difficulty: Difficulty,
    solved_tag_counts: Counter,
    weak_tag_counts: Counter,
    company_freq: float | None,
) -> float:
    score = 0.0
    tags = [t.strip() for t in problem.tags.split(",")] if problem.tags else []

    # 1. Weak topic boost
    for tag in tags:
        weak_count = weak_tag_counts.get(tag, 0)
        solved_count = solved_tag_counts.get(tag, 0)
        if weak_count > solved_count:
            score += 15

    # 2. Topic coverage — favor less-seen topics
    for tag in tags:
        total_exposure = solved_tag_counts.get(tag, 0) + weak_tag_counts.get(tag, 0)
        if total_exposure == 0:
            score += 10
        elif total_exposure < 3:
            score += 5

    # 3. Difficulty match
    if problem.difficulty == target_difficulty:
        score += 8
    elif problem.difficulty in _acceptable_difficulties(target_difficulty):
        score += 3

    # 4. Company relevance
    if company_freq is not None:
        score += 20 + (company_freq * 10)

    # 5. Acceptance rate sweet spot
    if problem.ac_rate is not None:
        if 40 <= problem.ac_rate <= 70:
            score += 5
        elif problem.ac_rate > 70:
            score += 2

    # 6. Classic problems bonus
    if problem.leetcode_id <= 100:
        score += 6
    elif problem.leetcode_id <= 300:
        score += 3

    return score


def _pick_difficulty(solved_counts: dict[Difficulty, int]) -> Difficulty:
    easy = solved_counts.get(Difficulty.EASY, 0)
    medium = solved_counts.get(Difficulty.MEDIUM, 0)

    if easy < 5:
        return Difficulty.EASY
    elif easy < 15 or medium < 5:
        return random.choice([Difficulty.EASY, Difficulty.MEDIUM])
    elif medium < 20:
        return Difficulty.MEDIUM
    else:
        return random.choice([Difficulty.MEDIUM, Difficulty.HARD])


def _acceptable_difficulties(target: Difficulty) -> list[Difficulty]:
    idx = DIFFICULTY_ORDER.index(target)
    result = [target]
    if idx > 0:
        result.append(DIFFICULTY_ORDER[idx - 1])
    if idx < len(DIFFICULTY_ORDER) - 1:
        result.append(DIFFICULTY_ORDER[idx + 1])
    return result


async def _pick_unsolved_problem(db: AsyncSession, user_id: int) -> Problem | None:
    unsolved_ids_result = await db.execute(
        select(Submission.problem_id).where(
            Submission.user_id == user_id,
            Submission.status.in_([SubmissionStatus.SENT, SubmissionStatus.ATTEMPTED]),
        )
    )
    unsolved_ids = [row[0] for row in unsolved_ids_result.fetchall()]
    if not unsolved_ids:
        return None
    result = await db.execute(
        select(Problem).where(Problem.id.in_(unsolved_ids)).order_by(func.random()).limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Interview prep: topic coverage analysis + gap-filling recommendations
# ---------------------------------------------------------------------------

# Must-know topics for coding interviews, with minimum problem counts per difficulty
INTERVIEW_TOPICS = {
    "Array":                  {"easy": 3, "medium": 4, "hard": 1},
    "String":                 {"easy": 2, "medium": 3, "hard": 1},
    "Hash Table":             {"easy": 2, "medium": 3, "hard": 0},
    "Two Pointers":           {"easy": 2, "medium": 3, "hard": 1},
    "Sliding Window":         {"easy": 1, "medium": 3, "hard": 1},
    "Stack":                  {"easy": 2, "medium": 2, "hard": 1},
    "Linked List":            {"easy": 2, "medium": 3, "hard": 1},
    "Binary Search":          {"easy": 2, "medium": 3, "hard": 1},
    "Tree":                   {"easy": 2, "medium": 3, "hard": 1},
    "Binary Tree":            {"easy": 2, "medium": 3, "hard": 1},
    "Graph":                  {"easy": 1, "medium": 3, "hard": 1},
    "BFS":                    {"easy": 1, "medium": 2, "hard": 1},
    "DFS":                    {"easy": 1, "medium": 2, "hard": 1},
    "Dynamic Programming":    {"easy": 1, "medium": 4, "hard": 2},
    "Greedy":                 {"easy": 1, "medium": 3, "hard": 1},
    "Backtracking":           {"easy": 0, "medium": 3, "hard": 1},
    "Heap (Priority Queue)":  {"easy": 1, "medium": 2, "hard": 1},
    "Trie":                   {"easy": 0, "medium": 2, "hard": 1},
    "Sorting":                {"easy": 2, "medium": 2, "hard": 0},
    "Bit Manipulation":       {"easy": 2, "medium": 1, "hard": 0},
    "Math":                   {"easy": 2, "medium": 1, "hard": 0},
}


def _get_lc_tag_counts(user: User) -> dict[str, int]:
    """Parse the user's LeetCode per-tag solve counts from the stored JSON."""
    if not user.lc_tag_counts:
        return {}
    try:
        return json.loads(user.lc_tag_counts)
    except (json.JSONDecodeError, TypeError):
        return {}


async def get_topic_coverage(db: AsyncSession, user: User) -> dict:
    """Analyze the user's solve history and return a topic-by-topic coverage map.

    Uses the REAL LeetCode per-tag solve counts (e.g. Array: 66) when available,
    not just the handful of recent submissions we tracked locally.
    """
    # Real LeetCode tag counts (e.g. {"Array": 66, "Dynamic Programming": 15})
    lc_tags = _get_lc_tag_counts(user)

    # Also get local DB counts as fallback / supplement
    solved_result = await db.execute(
        select(Submission.problem_id).where(
            Submission.user_id == user.id,
            Submission.status == SubmissionStatus.SOLVED,
        )
    )
    solved_ids = {row[0] for row in solved_result.fetchall()}

    solved_problems = []
    if solved_ids:
        result = await db.execute(
            select(Problem).where(Problem.id.in_(solved_ids))
        )
        solved_problems = result.scalars().all()

    # Count local DB solved per topic
    local_tag_counts: dict[str, int] = {}
    for p in solved_problems:
        if not p.tags:
            continue
        for tag in (t.strip() for t in p.tags.split(",")):
            local_tag_counts[tag] = local_tag_counts.get(tag, 0) + 1

    # Use the HIGHER of LeetCode count vs local count for each topic
    # (LeetCode is authoritative, but local may have recent solves not yet synced)
    effective_tag_counts: dict[str, int] = {}
    all_topics = set(list(INTERVIEW_TOPICS.keys()) + list(lc_tags.keys()) + list(local_tag_counts.keys()))
    for topic in all_topics:
        effective_tag_counts[topic] = max(lc_tags.get(topic, 0), local_tag_counts.get(topic, 0))

    # Build coverage map — we use total tag counts (not per-difficulty) since
    # LeetCode API doesn't break tags down by difficulty
    topics_coverage = {}
    total_target = 0
    total_met = 0

    for topic, targets in INTERVIEW_TOPICS.items():
        total_topic_target = targets["easy"] + targets["medium"] + targets["hard"]
        total_topic_solved = effective_tag_counts.get(topic, 0)

        # Distribute solved across difficulties proportionally for display
        # (we know the total but not the per-difficulty breakdown from LC API)
        topic_data = {}
        topic_complete = total_topic_solved >= total_topic_target
        for diff in ("easy", "medium", "hard"):
            target = targets[diff]
            # Estimate per-difficulty: proportional to target ratio
            if total_topic_target > 0:
                estimated = min(target, round(total_topic_solved * target / total_topic_target))
            else:
                estimated = 0
            topic_data[diff] = {"solved": estimated, "target": target}
            total_target += target
            total_met += min(estimated, target)

        topic_data["complete"] = topic_complete
        topic_data["total_solved"] = total_topic_solved
        topics_coverage[topic] = topic_data

    coverage_pct = (total_met / total_target * 100) if total_target > 0 else 0

    # Rank topics by gap
    topic_gaps = []
    for topic, targets in INTERVIEW_TOPICS.items():
        total_target_for_topic = targets["easy"] + targets["medium"] + targets["hard"]
        solved_for_topic = effective_tag_counts.get(topic, 0)
        gap = max(0, total_target_for_topic - solved_for_topic)
        topic_gaps.append((topic, gap))

    topic_gaps.sort(key=lambda x: x[1], reverse=True)
    weakest = [t for t, g in topic_gaps if g > 0][:5]
    strongest = [t for t, g in reversed(topic_gaps) if g == 0][:5]

    total_solved = user.lc_total_solved if user.lc_total_solved > 0 else len(solved_ids)

    return {
        "topics": topics_coverage,
        "total_solved": total_solved,
        "coverage_pct": round(coverage_pct, 1),
        "weakest_topics": weakest,
        "strongest_topics": strongest,
    }


async def build_daily_plan(
    db: AsyncSession,
    user: User,
    count: int = 6,
) -> list[dict]:
    """Build a daily plan of `count` problems with varied topics and difficulties.

    Returns a list of dicts:
        [{"problem": Problem, "reason": str, "slot": str}, ...]

    Strategy:
    - Difficulty mix based on user level: beginner (3E/2M/1H), intermediate (2E/3M/1H), advanced (1E/3M/2H)
    - Each problem from a different primary topic — no two problems share the same main topic
    - Prioritizes interview gaps, company problems, weak areas
    - Never repeats solved or already-sent-today problems
    """
    # Gather user history
    subs_result = await db.execute(
        select(Submission).where(Submission.user_id == user.id)
    )
    submissions = subs_result.scalars().all()
    solved_ids = {s.problem_id for s in submissions if s.status == SubmissionStatus.SOLVED}
    all_touched_ids = {s.problem_id for s in submissions}

    # Use REAL LeetCode stats for level detection
    lc_tags = _get_lc_tag_counts(user)
    total_solved = user.lc_total_solved if user.lc_total_solved > 0 else len(solved_ids)
    easy_count = user.lc_easy_solved if user.lc_total_solved > 0 else 0
    medium_count = user.lc_medium_solved if user.lc_total_solved > 0 else 0

    # Determine difficulty mix based on real LC stats
    if total_solved < 15 or easy_count < 10:
        diff_slots = _distribute_difficulties(count, easy_pct=0.5, medium_pct=0.33, hard_pct=0.17)
    elif medium_count < 30:
        diff_slots = _distribute_difficulties(count, easy_pct=0.17, medium_pct=0.5, hard_pct=0.33)
    else:
        diff_slots = _distribute_difficulties(count, easy_pct=0.17, medium_pct=0.5, hard_pct=0.33)

    # Rank topics by gap using real LC tag counts
    topic_gaps: list[tuple[str, int]] = []
    for topic, targets in INTERVIEW_TOPICS.items():
        total_target = targets["easy"] + targets["medium"] + targets["hard"]
        solved_for_topic = lc_tags.get(topic, 0)
        gap = max(0, total_target - solved_for_topic)
        topic_gaps.append((topic, gap))
    topic_gaps.sort(key=lambda x: x[1], reverse=True)

    # Company boost
    company_problem_ids: set[int] = set()
    if user.target_company:
        cp_result = await db.execute(
            select(CompanyProblem.problem_id).where(
                CompanyProblem.company == user.target_company.lower()
            )
        )
        company_problem_ids = {row[0] for row in cp_result.fetchall()}

    # Pick problems — one per topic, matching the difficulty slot
    plan: list[dict] = []
    used_ids: set[int] = set()
    used_topics: set[str] = set()
    exclude_ids = all_touched_ids | used_ids

    for target_diff in diff_slots:
        picked = False
        # Try topics in gap order, skip already-used topics
        for topic, gap in topic_gaps:
            if topic in used_topics:
                continue

            query = select(Problem).where(
                Problem.tags.like(f"%{topic}%"),
                Problem.difficulty == target_diff,
            )
            exclude = exclude_ids | used_ids
            if exclude:
                query = query.where(Problem.id.not_in(exclude))

            result = await db.execute(query)
            candidates = result.scalars().all()
            if not candidates:
                continue

            # Score candidates
            def pick_key(p: Problem) -> tuple:
                company = 0 if p.id in company_problem_ids else 1
                classic = 0 if p.leetcode_id <= 300 else 1
                ac = 0 if p.ac_rate and 35 <= p.ac_rate <= 65 else 1
                return (company, classic, ac, p.leetcode_id)

            candidates.sort(key=pick_key)
            chosen = candidates[0]

            # Determine reason
            reason = f"{topic} practice"
            if gap > 0:
                total_target = sum(INTERVIEW_TOPICS[topic].values())
                solved_for = lc_tags.get(topic, 0)
                reason = f"Fills {topic} gap ({solved_for}/{total_target} solved)"
            if chosen.id in company_problem_ids:
                reason += f" | {user.target_company} tagged"

            plan.append({
                "problem": chosen,
                "reason": reason,
                "slot": target_diff.value,
            })
            used_ids.add(chosen.id)
            used_topics.add(topic)
            picked = True
            break

        if not picked:
            # Fallback: any unsent problem at this difficulty
            query = select(Problem).where(Problem.difficulty == target_diff)
            exclude = exclude_ids | used_ids
            if exclude:
                query = query.where(Problem.id.not_in(exclude))
            query = query.order_by(func.random()).limit(1)
            result = await db.execute(query)
            fallback = result.scalar_one_or_none()
            if fallback:
                plan.append({
                    "problem": fallback,
                    "reason": "Extra practice",
                    "slot": target_diff.value,
                })
                used_ids.add(fallback.id)

    return plan


def _distribute_difficulties(
    count: int,
    easy_pct: float,
    medium_pct: float,
    hard_pct: float,
) -> list[Difficulty]:
    """Distribute `count` slots across difficulties, ensuring at least 1 of each when count >= 3."""
    easy_n = max(1, round(count * easy_pct))
    hard_n = max(1, round(count * hard_pct)) if count >= 3 else 0
    medium_n = count - easy_n - hard_n
    if medium_n < 0:
        medium_n = 0
        easy_n = count - hard_n

    slots = (
        [Difficulty.EASY] * easy_n
        + [Difficulty.MEDIUM] * medium_n
        + [Difficulty.HARD] * hard_n
    )
    # Interleave so it's not all easy then all medium — alternate
    random.shuffle(slots)
    return slots


async def get_interview_problem(db: AsyncSession, user: User) -> Problem | None:
    """Recommend the best unsolved problem for interview prep.

    Strategy:
    1. Sync solve history to know what's already done
    2. Find the weakest topic (biggest gap between target and solved)
    3. Within that topic, pick an unsolved problem at the needed difficulty
    4. Never recommend something the user has already solved or been sent
    """
    # Get all problem IDs the user has interacted with
    subs_result = await db.execute(
        select(Submission).where(Submission.user_id == user.id)
    )
    submissions = subs_result.scalars().all()

    all_touched_ids = {s.problem_id for s in submissions}

    # Use real LeetCode tag counts
    lc_tags = _get_lc_tag_counts(user)

    # Find topics with gaps (using real LC counts)
    gaps: list[tuple[str, str, int]] = []  # (topic, difficulty, gap)
    for topic, targets in INTERVIEW_TOPICS.items():
        total_target = targets["easy"] + targets["medium"] + targets["hard"]
        solved_for_topic = lc_tags.get(topic, 0)
        if solved_for_topic < total_target:
            # We don't know per-difficulty breakdown, so pick the difficulty
            # most needed for interview balance
            for diff in ("medium", "hard", "easy"):
                gaps.append((topic, diff, total_target - solved_for_topic))

    if not gaps:
        # All interview targets met — fall back to general recommender
        return await get_recommended_problem(db, user)

    # Sort by gap descending, prefer medium > easy > hard for interview balance
    diff_priority = {"medium": 0, "easy": 1, "hard": 2}
    gaps.sort(key=lambda x: (-x[2], diff_priority.get(x[1], 1)))

    diff_map = {"easy": Difficulty.EASY, "medium": Difficulty.MEDIUM, "hard": Difficulty.HARD}

    # Company boost
    company_problem_ids: set[int] = set()
    if user.target_company:
        cp_result = await db.execute(
            select(CompanyProblem.problem_id).where(
                CompanyProblem.company == user.target_company.lower()
            )
        )
        company_problem_ids = {row[0] for row in cp_result.fetchall()}

    # Try top gaps until we find a candidate
    for topic, diff_str, gap in gaps[:10]:
        query = select(Problem).where(
            Problem.tags.like(f"%{topic}%"),
            Problem.difficulty == diff_map[diff_str],
        )
        if all_touched_ids:
            query = query.where(Problem.id.not_in(all_touched_ids))

        result = await db.execute(query)
        candidates = result.scalars().all()

        if not candidates:
            continue

        # Prefer company-tagged problems, then classic (low ID), then by acceptance rate
        def sort_key(p: Problem) -> tuple:
            company_boost = 0 if p.id in company_problem_ids else 1
            classic_boost = 0 if p.leetcode_id <= 300 else 1
            ac_sweet = 0 if p.ac_rate and 35 <= p.ac_rate <= 65 else 1
            return (company_boost, classic_boost, ac_sweet, p.leetcode_id)

        candidates.sort(key=sort_key)
        return candidates[0]

    # If no gaps could be filled with untouched problems, fall back
    return await get_recommended_problem(db, user)
