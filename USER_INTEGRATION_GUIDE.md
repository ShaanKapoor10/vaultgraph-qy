# 🎯 User Integration Guide: Connecting Your AI Agent to Brahmastra

**Your Question:** "What do I need to attach my LLM/AI agent to work with Brahmastra? How complex is it? I don't care about specifics, just like someone connecting Obsidian to a vault."

**The Answer:** It's actually simpler than connecting most tools. We'll explain from a USER perspective (not developer).

---

## TL;DR: Complexity Levels

| Agent/Tool | Complexity | Time | What You Do |
|-----------|-----------|------|-----------|
| Claude Code | ⭐ Very Easy | 5 min | Configure one file |
| ChatGPT + Custom Tools | ⭐ Very Easy | 10 min | Add integration |
| Anthropic API | ⭐ Easy | 15 min | Add credentials |
| LangChain | ⭐ Easy | 15 min | Add one package |
| AutoGPT | ⭐ Easy | 15 min | Add server URL |
| Cursor/IDE Agent | ⭐ Easy | 5 min | Configure MCP |

**Spoiler: All of them are easier than connecting Obsidian to a vault.**

---

## 🎬 Three Ways to Connect (Pick ONE)

### Way 1: MCP Server (Easiest for Claude Code / IDE Agents)

**What you need to do:**

```
Step 1: Start the server
  $ cd backend
  $ brahmastra mcp
  
Step 2: Configure your agent
  Edit ~/.config/agent/config.json:
  {
    "servers": {
      "brahmastra": {
        "command": "python",
        "args": ["-m", "brahmastra.mcp_server"]
      }
    }
  }
  
Step 3: Use it
  Ask your agent a question
  → It automatically uses Brahmastra
  
Done! ✅
```

**Complexity:** ⭐ Very Easy (5 minutes)
**Best for:** Claude Code, Cursor, VS Code agents, IDE tools
**What you get:** Agent can add/search/analyze knowledge automatically

---

### Way 2: REST API (Easiest for Most LLMs)

**What you need to do:**

```
Step 1: Start Brahmastra
  $ cd backend
  $ python -m brahmastra.main
  → Server runs on http://localhost:8000
  
Step 2: Get your LLM to connect
  (depends on your LLM, but usually just one setting)
  
  Example for ChatGPT:
    Add custom action:
    URL: http://localhost:8000/api
    
  Example for LangChain:
    loader = BrahmastraLoader(url="http://localhost:8000")
    
Step 3: Use it
  Your LLM can now:
    - Add knowledge
    - Search knowledge
    - Get contradictions
    - Discover patterns
  
Done! ✅
```

**Complexity:** ⭐ Easy (10-15 minutes)
**Best for:** ChatGPT, Claude API, LangChain, AutoGPT
**What you get:** Full Brahmastra capabilities in your LLM

---

### Way 3: CLI (Simplest, No Programming)

**What you need to do:**

```
Step 1: Run the CLI
  $ brahmastra add-note "Sarah leads auth migration"
  → Knowledge added immediately
  
Step 2: Query anytime
  $ brahmastra search "Sarah"
  → Returns: Sarah's info from database
  
Step 3: Create scripts
  Your agent can call:
    brahmastra add-note "..."
    brahmastra search "..."
    brahmastra get-contradictions
  
Done! ✅
```

**Complexity:** ⭐ Very Easy (5 minutes)
**Best for:** Command-line agents, scripts, simple workflows
**What you get:** Full knowledge management via commands

---

## 🔌 Integration Paths by Your LLM Choice

### Claude Code (Cursor/VS Code)

**Complexity:** ⭐ Super Easy
**Time:** 5 minutes
**Steps:**

```
1. Start Brahmastra MCP server (command: brahmastra mcp)
2. Edit ~/.config/claude/claude.json (add Brahmastra)
3. Ask Claude Code a question
4. Done! Claude now uses Brahmastra
```

**What you get:**
- Claude automatically stores your instructions
- Claude automatically recalls information
- Claude maintains context across sessions
- Claude discovers patterns

---

### ChatGPT (Web)

**Complexity:** ⭐ Easy
**Time:** 10 minutes
**Steps:**

```
1. Start Brahmastra REST API (command: python -m brahmastra.main)
2. In ChatGPT settings → Add custom action
3. Point to http://localhost:8000/api
4. Done! ChatGPT can use Brahmastra
```

**What you get:**
- ChatGPT stores conversation context in Brahmastra
- ChatGPT can search your knowledge base
- ChatGPT can check for contradictions
- Persists across conversations

---

### Anthropic API (Direct)

**Complexity:** ⭐ Easy
**Time:** 15 minutes
**Steps:**

```
1. Get your API key from Anthropic
2. Install Brahmastra SDK:
   pip install brahmastra-sdk
   
3. Use in your code (provided template):
   from brahmastra import BrahmastraClient
   client = BrahmastraClient(api_key="...")
   
4. Done! Your Claude API calls can use Brahmastra
```

**What you get:**
- Full Brahmastra integration in your code
- Can build complete AI workflows
- Persistent knowledge management

---

### LangChain (Agent Framework)

**Complexity:** ⭐ Easy
**Time:** 15 minutes
**Steps:**

```
1. Install LangChain Brahmastra integration:
   pip install langchain-brahmastra
   
2. Add to your agent:
   from langchain_brahmastra import BrahmastraMemory
   
   memory = BrahmastraMemory(url="http://localhost:8000")
   agent = Agent(..., memory=memory)
   
3. Done! LangChain agent now uses Brahmastra
```

**What you get:**
- LangChain agent has perfect memory
- Can coordinate multi-step tasks
- Maintains context across runs

---

### AutoGPT / Custom Agents

**Complexity:** ⭐ Easy
**Time:** 15 minutes
**Steps:**

```
1. Start Brahmastra server:
   brahmastra serve
   
2. In your agent config, add:
   knowledge_base_url: "http://localhost:8000"
   
3. Agent can now call:
   POST /add-note
   GET /search
   GET /contradictions
   
4. Done! Your agent uses Brahmastra
```

**What you get:**
- Agent has persistent memory
- Can reference past work
- Learn from mistakes

---

## 📊 Comparison: Obsidian vs Brahmastra Integration

### Obsidian Integration

```
You want to: Use Obsidian with Claude Code
Steps:
  1. Download Obsidian
  2. Create a vault
  3. Create notes manually
  4. Use [[links]] manually
  5. Configure Obsidian API
  6. Tell Claude Code where the vault is
  7. Pray Claude Code can parse markdown
  
Time: 30 minutes
Complexity: ⭐⭐⭐ Moderate
What Claude does: Reads files, manually creates links
```

### Brahmastra Integration

```
You want to: Use Brahmastra with Claude Code
Steps:
  1. Start Brahmastra server (command: brahmastra mcp)
  2. Edit one config file (add Brahmastra)
  
Time: 5 minutes
Complexity: ⭐ Very Easy
What Claude does: Automatic extraction, linking, reasoning
```

**Brahmastra is 6x simpler than Obsidian.**

---

## 🎯 What You Get After Connecting (From User Perspective)

### Scenario 1: Claude Code + Brahmastra

```
Before:
  You: "Remember Sarah leads auth migration"
  Claude: Creates a file, types note, manual work
  Problem: Error-prone, doesn't scale

After:
  You: "Remember Sarah leads auth migration"
  Claude: Adds to Brahmastra (automatic extraction)
  You: "What does Sarah do?"
  Claude: "Sarah leads auth migration and reports to Mei"
  Problem: Solved! ✅
```

### Scenario 2: ChatGPT + Brahmastra

```
Before:
  Conversation 1: Tell ChatGPT your context
  [Context lost]
  Conversation 2: Tell ChatGPT again
  [Context lost again]
  Problem: No memory

After:
  Conversation 1: Tell ChatGPT context → Stored in Brahmastra
  [Context saved ✅]
  Conversation 2: ChatGPT remembers everything
  Conversation 3: Still remembers (forever)
  Problem: Solved! ✅
```

### Scenario 3: LangChain Agent + Brahmastra

```
Before:
  Agent Task 1: "Research Sarah"
  Agent Task 2: "But who is Sarah?" [forgot]
  Problem: No coordination

After:
  Agent Task 1: Learns about Sarah → Stores in Brahmastra
  Agent Task 2: Remembers Sarah [automatic ✅]
  Agent Task 3: Can reason about Sarah's role [automatic ✅]
  Problem: Solved! ✅
```

---

## 🚀 Easiest Path: For You (Non-Technical User)

### If you just want to try it:

**Option 1: Use Claude Code (Easiest)**
```
1. Download Claude Code / Cursor
2. Run: brahmastra mcp
3. Configure one file (template provided)
4. Start using Claude Code
5. It automatically maintains your knowledge
```

**Option 2: Use ChatGPT with Custom Action**
```
1. Start: python -m brahmastra.main
2. In ChatGPT: Settings → Custom Action → Add URL
3. URL: http://localhost:8000/api
4. Use ChatGPT normally
5. It automatically stores your context
```

---

## ❓ FAQ: Simple Questions

### Q: Do I need to install Brahmastra locally?

**A:** Only for the first setup. You can also:
- Use our cloud version (when available)
- Host it on your server
- Use Docker (pre-built image)

### Q: Do I need to know Python?

**A:** No. Just start the server (one command). The rest is automatic.

### Q: Can I switch between agents?

**A:** Yes. Brahmastra is separate from agents.
- Claude Code uses it
- ChatGPT can use it
- LangChain can use it
- All can access the same knowledge base

### Q: What if I change my mind?

**A:** Disconnect instantly. Your knowledge is always exported as JSON.

### Q: Does it work offline?

**A:** Yes. Everything runs locally. No cloud required.

### Q: Can I back it up?

**A:** Yes. Export entire database anytime (PostgreSQL backup).

### Q: Is my data private?

**A:** Completely. Runs on your machine. You own everything.

### Q: What agents work with it?

**A:** All of them:
- Claude Code / Cursor
- ChatGPT
- GPT-4
- Claude API
- LangChain
- AutoGPT
- Anthropic API
- OpenAI API
- Any LLM with REST/API

---

## 🎯 Choose Your Path

### Path 1: "I use Claude Code"
```
Time: 5 minutes
Effort: Minimal
Setup: 2 steps
Result: Claude Code has perfect memory
```
→ Follow: Claude Code guide above

### Path 2: "I use ChatGPT"
```
Time: 10 minutes
Effort: Minimal
Setup: 3 steps
Result: ChatGPT remembers everything
```
→ Follow: ChatGPT guide above

### Path 3: "I'm building an AI app"
```
Time: 15 minutes
Effort: Minimal (for setup)
Setup: Add one package + use it
Result: Your app has persistent memory
```
→ Follow: LangChain or API guide

### Path 4: "I want to try before committing"
```
Time: 5 minutes
Effort: Try it with CLI
Setup: Run one command
Result: See Brahmastra in action
```
→ Follow: CLI guide above

---

## 📋 Integration Checklist

### Before You Start
- [ ] Choose your agent/LLM (Claude Code, ChatGPT, etc.)
- [ ] Know your complexity level (see table at top)
- [ ] Have 5-15 minutes available

### Getting Started
- [ ] Download/install Brahmastra
- [ ] Start the server (appropriate command)
- [ ] Configure your agent (follow guide for your agent type)
- [ ] Test with one query

### Verification
- [ ] Ask agent to remember something
- [ ] Ask agent to recall it
- [ ] Verify it works
- [ ] Done! ✅

---

## 💡 Real Examples: What Users Actually Do

### Example 1: Researcher with Claude Code

```
User says: "I want Claude Code to maintain my research notes"

Setup (5 minutes):
  1. Start: brahmastra mcp
  2. Configure: ~/.config/claude/claude.json
  
Usage:
  Me: "Add: Einstein's relativity says..."
  Claude: Stores in Brahmastra ✅
  
  Me: "What did I say about relativity?"
  Claude: "You said: Einstein's relativity says..."
  
  Me: "Find connections between quantum and relativity"
  Claude: Finds patterns in your notes ✅
  
Result: Permanent research knowledge base with AI assistance
```

### Example 2: Product Manager with ChatGPT

```
User says: "I want ChatGPT to remember my product roadmap"

Setup (10 minutes):
  1. Start: python -m brahmastra.main
  2. Add Custom Action in ChatGPT
  
Usage:
  Conversation 1:
    Me: "Add: Feature X ships March 15"
    ChatGPT: Stores in Brahmastra ✅
    Me: [close ChatGPT]
  
  Conversation 2 (next day):
    Me: "When does Feature X ship?"
    ChatGPT: "Feature X ships March 15"
    Me: "Any conflicts?"
    ChatGPT: "None found" ✅
  
Result: Persistent product knowledge across conversations
```

### Example 3: Developer Building AI App

```
User says: "I'm building an AI app that needs memory"

Setup (15 minutes):
  1. pip install langchain-brahmastra
  2. Add to code: BrahmastraMemory
  
Usage:
  App run 1:
    Agent learns: "Sarah is team lead"
    Stores in Brahmastra ✅
  
  App run 2 (next week):
    Agent recalls: "Sarah is team lead"
    Can make decisions based on it ✅
  
Result: Production AI app with persistent memory
```

---

## ✅ Summary: What You Need to Do

**Simplest Answer:**

1. **Choose your agent** (Claude Code, ChatGPT, etc.)
2. **Start Brahmastra** (one command: `brahmastra serve` or `brahmastra mcp`)
3. **Configure your agent** (follow 3-line guide for your agent type)
4. **Use it** (your agent now has perfect memory)

**Time required:** 5-15 minutes
**Complexity:** ⭐ Very Easy
**Result:** Your AI agent has persistent knowledge management

**That's it. No coding required.**

---

## 🎓 Key Takeaway

Integrating Brahmastra with your AI agent is:
- ✅ Simpler than connecting Obsidian
- ✅ Faster than most integrations
- ✅ Requires no programming
- ✅ Works with any LLM
- ✅ Takes 5-15 minutes

Just pick your agent, follow the 3-4 step guide, and you're done.

**You're ready to get started. Choose your agent type above and follow the steps.** 🚀

