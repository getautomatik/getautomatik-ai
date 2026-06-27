import anthropic
import os
from supabase import create_client
from dotenv import load_dotenv
import json

load_dotenv()

class BaseAgent:
    def __init__(self, name, role, tools):
        self.name = name
        self.role = role
        self.tools = tools
        self.claude = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        self.db = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    
    def think(self, context):
        tools_desc = "\n".join([f"- {t['name']}: {t['description']}" for t in self.tools])
        prompt = f"Sei {self.name}, {self.role}. Contesto: {json.dumps(context)}. Strumenti: {tools_desc}. Rispondi SOLO JSON con thought, tool, params, reason."
        response = self.claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        try:
            decision = json.loads(response.content[0].text)
        except:
            decision = {"thought": "fallback", "tool": None, "params": {}, "reason": "json error"}
        try:
            self.db.table("decisions").insert({"agent_name": self.name, "thought_process": decision.get("thought",""), "decision": decision.get("tool",""), "action_taken": "pending"}).execute()
            self.db.table("agents").update({"status": "thinking", "last_thought": decision.get("thought","")[:100]}).eq("name", self.name).execute()
        except:
            pass
        return decision
    
    def execute(self, tool_name, params):
        for tool in self.tools:
            if tool["name"] == tool_name:
                result = tool["function"](self.db, params)
                try:
                    self.db.table("agents").update({"status": "executed", "last_action": tool_name}).eq("name", self.name).execute()
                except:
                    pass
                return result
        return {"error": "Tool non trovato"}