"""Prompt templates for all agent AI tasks."""


def seo_analysis_prompt(page_title: str, page_url: str, keyword: str, content_snippet: str) -> str:
    return f"""Analyze this WordPress page and provide a specific, actionable SEO recommendation.

Page: {page_title}
URL: {page_url}
Target keyword: {keyword}
Content preview: {content_snippet[:500]}

Provide a 2-3 sentence recommendation focused on what specific changes would help this page rank higher for the target keyword. Be concrete and technical."""


def content_repurpose_linkedin(title: str, content: str, brand_voice: str) -> str:
    return f"""Transform this blog post into a LinkedIn post.

Brand voice: {brand_voice}
Blog title: {title}
Content: {content[:2000]}

Write a LinkedIn post that:
- Starts with a hook (not "I" or the blog title)
- Includes 3-5 key insights as bullet points
- Ends with a thought-provoking question
- Uses appropriate LinkedIn formatting
- Is 150-300 words
- Includes 3-5 relevant hashtags at the end"""


def content_repurpose_twitter(title: str, content: str, brand_voice: str) -> str:
    return f"""Transform this blog post into a Twitter/X thread.

Brand voice: {brand_voice}
Blog title: {title}
Content: {content[:2000]}

Write a Twitter thread with:
- Tweet 1: Hook (max 240 chars, numbered 1/)
- Tweets 2-5: Key insights (each max 240 chars)
- Final tweet: Call to action with link placeholder

Format as: [1/] ... [2/] ... etc."""


def weekly_report_prompt(site_name: str, metrics: dict) -> str:
    return f"""Generate a professional weekly performance report for {site_name}.

Metrics this week:
{metrics}

Write a concise executive summary (3-4 paragraphs) covering:
1. Overall health and key highlights
2. Issues found and resolved
3. SEO and content performance
4. Recommended focus areas for next week

Tone: professional, data-driven, actionable."""


def ab_variant_prompt(page_title: str, original_headline: str, page_type: str) -> str:
    return f"""Generate an A/B test variant for this WordPress page.

Page: {page_title} ({page_type})
Original headline: {original_headline}

Write ONE alternative headline that:
- Tests a different emotional angle (urgency vs. curiosity vs. benefit-led)
- Is roughly the same length as the original
- Is compelling and click-worthy
- Maintains brand professionalism

Return only the alternative headline text, nothing else."""


def traffic_prediction_prompt(
    site_name: str, history_csv: str, horizon_days: int, has_search_data: bool = False,
) -> str:
    search_columns_note = """
The last four columns come from Google Search Console (organic Google search only):
  impressions  — times the site appeared in search results that day
  clicks       — clicks from those results
  ctr_pct      — clicks/impressions as a percentage
  avg_position — impression-weighted average ranking (LOWER is better)
A BLANK in these columns means Search Console had not finalized that day yet
(it runs 2-3 days behind analytics) — treat it as UNKNOWN, never as zero.
""" if has_search_data else ""

    search_analysis = """
5. LEADING INDICATORS from the search columns — impressions move BEFORE
   pageviews do, so use them to anticipate turns rather than just describe
   the past:
   - impressions trending up while clicks/pageviews stay flat  -> traffic likely to rise; rankings or demand are improving first
   - impressions trending down                                 -> forecast a decline even if pageviews look stable today
   - impressions steady but ctr_pct falling                    -> a SERP/snippet/competitor problem, not a demand problem
   - avg_position worsening (rising number)                    -> ranking slide; expect impressions then clicks to follow
   Weight these leading signals ahead of raw pageview momentum where they disagree.
""" if has_search_data else ""

    narrative_spec = (
        "<2-3 paragraphs: trend direction, seasonality patterns, key risks, and one "
        "specific actionable opportunity. Explicitly separate DEMAND changes (impressions) "
        "from CONVERSION changes (CTR/position), and say which is driving the forecast>"
        if has_search_data else
        "<2-3 paragraphs: trend direction, seasonality patterns, key risks, and one specific actionable opportunity>"
    )

    return f"""You are a web analytics forecasting model. Analyze the traffic history for the site "{site_name}" and predict the next {horizon_days} days.

HISTORICAL DATA (CSV, most recent last):
{history_csv}
{search_columns_note}
Analyze for:
1. Overall trend (growing / declining / flat — estimate % change per week)
2. Day-of-week seasonality (which days are consistently higher or lower)
3. Any weekly or monthly cycles visible in the data
4. Recent anomalies or inflection points in the last 14 days
{search_analysis}
Return a JSON object with EXACTLY this schema — no extra keys, no markdown:
{{
  "daily_forecasts": [
    {{"date": "YYYY-MM-DD", "base": <int>, "optimistic": <int>, "pessimistic": <int>}}
  ],
  "anomalies": [
    {{"date": "YYYY-MM-DD", "type": "spike|drop|trend_break", "description": "<string>", "severity": "low|medium|high"}}
  ],
  "narrative": "{narrative_spec}"
}}

Rules:
- daily_forecasts must have EXACTLY {horizon_days} entries, consecutive starting from tomorrow
- base values must be integers grounded in recent daily averages — do not invent large numbers
- optimistic = base × 1.15 to 1.25 (widen to 1.30 for 30-day horizon)
- pessimistic = base × 0.75 to 0.85 (widen to 0.70 for 30-day horizon)
- anomalies: only flag dates where actual deviates > 30% from the rolling average — empty array is fine
- narrative: be specific about this site's data, not generic; mention actual numbers"""
