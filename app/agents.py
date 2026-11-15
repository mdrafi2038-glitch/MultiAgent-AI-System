from dataclasses import dataclass

@dataclass(frozen=True)
class Agent:
    key: str
    name: str
    role: str
    instruction: str

AGENTS = {
    "planner": Agent("planner", "Planner", "Task planning", "Break the user's goal into a short, ordered plan. Identify useful specialists. Do not claim to have performed actions."),
    "research": Agent("research", "Research Agent", "Research and analysis", "Analyze the task, identify facts needing verification, and provide a careful brief. Never invent sources or claim live browsing."),
    "writer": Agent("writer", "Writer Agent", "Writing", "Produce clear, useful writing suited to the requested format and audience."),
    "coder": Agent("coder", "Coding Agent", "Software development", "Design or write code when requested. Explain assumptions, risks, and how to test. Do not claim code was deployed or executed."),
    "data": Agent("data", "Data Agent", "Data analysis", "Structure and analyze supplied data. Show assumptions and calculations."),
    "reviewer": Agent("reviewer", "QA Reviewer", "Quality assurance", "Review correctness, completeness, unsupported claims, security, and clarity. Return a corrected answer and list limitations."),
}

def choose_agents(goal: str) -> list[str]:
    text = goal.lower()
    selected = []

    # Match natural task wording and explicit agent names, including Bengali prompts.
    if any(x in text for x in ("research", "research agent", "find", "latest", "compare", "source", "look up", "বাজার বিশ্লেষণ", "তথ্য খুঁজ")):
        selected.append("research")
    if any(x in text for x in ("writer", "writer agent", "writing", "write", "email", "post", "draft", "story", "rewrite", "পরিকল্পনা লিখ", "রিপোর্ট লিখ")):
        selected.append("writer")
    if any(x in text for x in ("coder", "coding agent", "coding", "code", "html", "calculator", "app", "website", "bug", "program", "python", "github", "deploy", "ক্যালকুলেটর", "কোড")):
        selected.append("coder")
    if any(x in text for x in ("data", "data agent", "calculate", "calculation", "report", "sales", "analyze", "analysis", "হিসাব", "খরচ", "আয়", "লাভ")):
        selected.append("data")
    return selected or ["research", "writer"]
