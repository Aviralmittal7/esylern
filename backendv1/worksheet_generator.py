"""
LLM-backed worksheet text generation (via Groq).

Robustness measures over the original version:
  - Retries transient errors (timeouts, rate limits, 5xx) with backoff.
  - Falls back through a list of models if the primary model has been
    decommissioned (Groq retires model IDs periodically -- this happened
    to the original hardcoded "llama3-8b-8192" model, which no longer
    works at all).
  - Validates the shape of the response (must contain the answer-key
    marker and a non-trivial amount of text) and retries once with a
    corrective nudge if the model didn't follow instructions.
  - Asks the model to stick to plain ASCII punctuation, which keeps the
    PDF renderer's job simple and output visually consistent (smart
    quotes/em-dashes from an LLM are otherwise extremely common).
  - Accepts recently-sent topics so the prompt can nudge the model away
    from repeating itself across consecutive worksheets.
"""
import logging
import time

from groq import Groq, APIStatusError, APIConnectionError, APITimeoutError

import config

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = Groq(api_key=config.GROQ_API_KEY, timeout=config.GROQ_REQUEST_TIMEOUT)
    return _client


ANSWER_KEY_MARKER = "---ANSWER KEY---"


def _build_prompt(grade, subject, topic, difficulty, avoid_topics=None):
    difficulty_guidance = {
        "easier": "Use simpler vocabulary and smaller numbers; build confidence.",
        "challenge": "Push the student with multi-step reasoning and trickier wording.",
        "review": "Mix in a couple of basics from earlier in the syllabus as a refresher.",
        "normal": "Target a typical difficulty for this grade level.",
    }.get(difficulty, "Target a typical difficulty for this grade level.")

    avoid_clause = ""
    if avoid_topics:
        avoid_clause = (
            "\nThe student already received worksheets on these topics recently -- "
            f"vary the angle, examples, or sub-skill rather than repeating them verbatim: "
            f"{', '.join(avoid_topics)}.\n"
        )

    return f"""You are a friendly educational worksheet creator writing for parents to print and use at home with their children.

Your task is to create a single-page worksheet for a {grade} student on {subject}, specifically focused on the topic: "{topic}".

Set the difficulty level to {difficulty}. {difficulty_guidance}

{avoid_clause}

Structure the worksheet with exactly 20 activities organized in this repeating cycle:
1. A short-answer question
2. A multiple-choice question with options labeled A through D
3. A problem-solving task
4. A creative or analytical exercise

Repeat this 5-time cycle to reach 20 total activities.

Make the worksheet creative, fun, and engaging for children while keeping instructions clear enough for a parent to read aloud without prior subject knowledge.

Formatting requirements (strictly follow these):
- Use only plain ASCII characters: straight apostrophes (') and straight quotes (" "), hyphens (-) instead of em-dashes, and the letter x for multiplication (write "4 x 3" not "4 × 3")
- Do not use markdown formatting—no bold, no hashtags, no special bullet symbols (plain dashes only if needed)
- Number each activity clearly (1., 2., 3., ... 20.)
- Write all instructions in clear, conversational language a parent can easily read aloud to their child
- Keep the entire worksheet to one printable page

After the final activity (20.), add a blank line, then output this exact marker on its own line:

ANSWER KEY

Immediately follow with the answer key, labeling each answer to match its activity number. For multiple-choice questions, provide the correct letter (A, B, C, or D) and the correct answer. For short-answer and problem-solving activities, provide clear, concise correct answers. For creative or analytical exercises, provide example answers or evaluation criteria.

Now generate the worksheet:"""


def _is_well_formed(full_text):
    if not full_text or len(full_text.strip()) < 40:
        return False
    if ANSWER_KEY_MARKER not in full_text:
        return False
    student_part, _, answer_part = full_text.partition(ANSWER_KEY_MARKER)
    return len(student_part.strip()) > 20 and len(answer_part.strip()) > 5


def _call_groq(prompt, model):
    client = _get_client()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=config.GROQ_TEMPERATURE,
        max_tokens=config.GROQ_MAX_TOKENS,
    )
    return response.choices[0].message.content


def _generate_with_fallback(prompt):
    """Try the primary model, then each fallback model, retrying transient
    errors on each before moving to the next model."""
    models_to_try = [config.GROQ_MODEL] + [
        m for m in config.GROQ_MODEL_FALLBACKS if m != config.GROQ_MODEL
    ]
    last_error = None

    for model in models_to_try:
        for attempt in range(1, config.GROQ_MAX_RETRIES + 1):
            try:
                text = _call_groq(prompt, model)
                if text:
                    return text, model
                last_error = RuntimeError("Empty response from model")
            except APIStatusError as e:
                last_error = e
                # 4xx errors other than rate limiting (429) usually mean the
                # model id itself is bad (e.g. decommissioned) -- no point
                # retrying the same model, move on to the next one.
                status = getattr(e, "status_code", None)
                if status and 400 <= status < 500 and status != 429:
                    logger.warning("Model %s rejected the request (HTTP %s): %s -- trying next model",
                                   model, status, e)
                    break
                logger.warning("Groq API error on model %s (attempt %s/%s): %s",
                                model, attempt, config.GROQ_MAX_RETRIES, e)
            except (APIConnectionError, APITimeoutError) as e:
                last_error = e
                logger.warning("Groq connection issue on model %s (attempt %s/%s): %s",
                                model, attempt, config.GROQ_MAX_RETRIES, e)
            except Exception as e:  # noqa: BLE001 - last-resort safety net
                last_error = e
                logger.warning("Unexpected error calling Groq with model %s (attempt %s/%s): %s",
                                model, attempt, config.GROQ_MAX_RETRIES, e)

            if attempt < config.GROQ_MAX_RETRIES:
                time.sleep(min(2 ** attempt, 10))

    raise RuntimeError(
        f"All Groq models failed ({', '.join(models_to_try)}). Last error: {last_error}"
    )


def generate_worksheet_text(grade, subject, topic, difficulty, avoid_topics=None):
    """Returns (student_text, answer_key, model_used).

    Raises RuntimeError if every model/retry attempt is exhausted, so the
    caller (scheduler.process_parent) can log and retry/back off rather
    than silently sending a broken worksheet.
    """
    prompt = _build_prompt(grade, subject, topic, difficulty, avoid_topics)
    full_text, model_used = _generate_with_fallback(prompt)

    if not _is_well_formed(full_text):
        # One corrective retry: be explicit about what was missing.
        logger.info("First worksheet generation was malformed, retrying with a corrective nudge.")
        corrective_prompt = prompt + (
            f"\n\nIMPORTANT: Your previous response did not include the exact line "
            f"'{ANSWER_KEY_MARKER}' separating the worksheet from the answer key. "
            f"Make sure to include it exactly once, with both sections non-empty."
        )
        full_text, model_used = _generate_with_fallback(corrective_prompt)

    if ANSWER_KEY_MARKER in full_text:
        student_part, _, answer_part = full_text.partition(ANSWER_KEY_MARKER)
        student_part, answer_part = student_part.strip(), answer_part.strip()
        if not answer_part:
            answer_part = "Answer key was not generated for this worksheet. Please review the activities together."
        return student_part, answer_part, model_used

    logger.warning("Worksheet generated without an answer-key marker after retry; sending as-is.")
    return full_text.strip(), "Answer key was not generated for this worksheet. Please review the activities together.", model_used
