import os
import json
from dotenv import load_dotenv

load_dotenv()


class BaseAgent:
    """Legacy agent wrapper kept for compatibility.

    The production revenue loop no longer uses autonomous agent decision making.
    Imports for Anthropic and Supabase stay lazy so local syntax/smoke tests do not
    require installing production dependencies.
    """

    def __init__(self, name, role, tools):
        self.name = name
        self.role = role
        self.tools = tools
        self.claude = None
        self.db = None
        try:
            import anthropic
            self.claude = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        except Exception:
            pass
        try:
            from supabase import create_client
            self.db = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        except Exception:
            pass

    def think(self, context):
        if not self.claude:
            return {"thought": "AI client unavailable", "tool": None, "params": {}, "reason": "missing anthropic"}
        tools_desc = "\n".join([f"- {t['name']}: {t['description']}" for t in self.tools])
        prompt = f"Sei {self.name}, {self.role}. Contesto: {json.dumps(context)}. Strumenti: {tools_desc}. Rispondi SOLO JSON con thought, tool, params, reason."
        response = self.claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            decision = json.loads(response.content[0].text)
        except Exception:
            decision = {"thought": "fallback", "tool": None, "params": {}, "reason": "json error"}
        try:
            if self.db:
                self.db.table("decisions").insert({
                    "agent_name": self.name,
                    "thought_process": decision.get("thought", ""),
                    "decision": decision.get("tool", ""),
                    "action_taken": "pending",
                }).execute()
                self.db.table("agents").update({
                    "status": "thinking",
                    "last_thought": decision.get("thought", "")[:100],
                }).eq("name", self.name).execute()
        except Exception:
            pass
        return decision

    def execute(self, tool_name, params):
        for tool in self.tools:
            if tool["name"] == tool_name:
                result = tool["function"](self.db, params)
                try:
                    if self.db:
                        self.db.table("agents").update({"status": "executed", "last_action": tool_name}).eq("name", self.name).execute()
                except Exception:
                    pass
                return result
        return {"error": "Tool non trovato"}
