# **Engineering Autonomous Persistence: A Technical Analysis of the Carry Forward Decision Engine**

The landscape of artificial intelligence has undergone a fundamental shift from reactive, prompt-based interactions toward proactive, goal-oriented agentic workflows. As autonomous agents are increasingly tasked with complex, multi-step operations such as software development, research, and system administration, the challenge of maintaining continuity across discrete computational sessions has become a primary bottleneck. The Carry Forward engine emerges as a specialized architectural solution to this problem, serving as a dedicated decision layer that governs session persistence. Unlike traditional task managers or context summarizers, Carry Forward focuses exclusively on the binary determination of whether an autonomous loop should spawn a subsequent session or halt operations based on empirical evidence of progress and system health .

This report examines the Carry Forward engine as a critical component of the Hermes Agent ecosystem developed by Nous Research. It provides an exhaustive analysis of the system's decision pipeline, its underlying data structures, and the recursive feedback loops that enable self-tuning behavior. By integrating version control progress, session activity metrics, and historical success patterns, Carry Forward addresses the "infinite loop" problem and "context window anxiety" that often plague autonomous agents. The following analysis explores the technical mechanisms through which Carry Forward achieves operational stability and optimizes resource allocation in agentic environments.

## **The Architecture of Session Continuation**

The primary function of the Carry Forward engine is to act as a governor for autonomous loops. When an agent concludes a session, the system must decide if the task is complete, if it has stalled, or if further work is required. This decision is not made by the language model itself, which is prone to hallucinations regarding its own progress, but by an external engine that evaluates the session's telemetry. The engine is explicitly defined not as a workflow orchestrator or a task manager, but as a decision engine for session continuation .

### **The Core Logic of the Decision Pipeline**

The decision to continue a session is the result of a five-stage pipeline. Each stage is designed to identify a specific failure mode or a signal of productivity. The logical flow ensures that a "Go" signal is only issued if all checks pass, thereby preventing unproductive cycles that consume compute tokens without making tangible progress.

#### **Stage 1: Dead Session Thrash Detection**

The first check in the pipeline addresses the phenomenon of "thrashing," where an agent enters a state of repeated, empty sessions. This often occurs due to configuration errors or the agent becoming stuck in a logic loop. The engine analyzes the lineage of the current session by traversing the parent\_session\_id chain in the state.db database.

The thrash detection logic utilizes two key thresholds: dead\_session\_threshold and dead\_lookback. By default, if three out of the last five sessions in the chain contain zero messages and zero tool calls, the engine identifies a thrash state and triggers a hard halt . This prevents the agent from wasting resources when it is clear that the loop is no longer interacting with the environment or the user.

#### **Stage 2: Version Control and Git Progress Validation**

A significant innovation in the Carry Forward engine is the use of Git as a proxy for productivity. In many autonomous tasks, particularly software engineering, progress is represented by file modifications and commits. The engine snapshots the Git HEAD at the beginning of a session chain and compares it to the state at the end of the evaluation period .

If the Git HEAD has not moved across a sequence of sessions defined by the git\_min\_sessions threshold (defaulting to 3), the engine concludes that the agent is "busy but unproductive." This stage is nested within the thrash detection logic; a Git stall is treated as a form of thrashing, even if the agent is exchanging messages. This prevents "planning loops" where the agent discusses work indefinitely without ever applying changes to the codebase .

#### **Stage 3: Pattern Recognition and Source Analysis**

The engine maintains a learned\_patterns table that tracks the continuation success rates of different session sources. For instance, an agent might discover that sessions initiated via the CLI have a 12% success rate, while those from a web interface might be higher. If the current session belongs to a source with a historical continuation rate below the continuation\_rate\_min (typically 15%), the engine issues a warning.

In addition to source rates, this stage evaluates "size effects." It has been observed that parent sessions with massive message counts (exceeding the parent\_size\_warning threshold, often set to 200\) tend to produce "dead" continuations due to context saturation and model amnesia. These patterns serve as guardrails rather than hard halts, informing the final decision output without necessarily blocking it.

#### **Stage 4: Persistent Blocker Management**

Autonomous agents frequently encounter blockers that require human intervention or external events to resolve, such as waiting for an API key or an ops team response. The Carry Forward engine allows these blockers to be logged with timestamps. The blocker\_halt\_hours threshold (default 4 hours) determines when a stale blocker should trigger a hard halt . This prevents the agent from repeatedly attempting a task that is fundamentally blocked by an external dependency.

#### **Stage 5: Session Activity Verification**

The final check in the pipeline evaluates the immediate activity of the session currently being analyzed. This check was introduced in version 5. after data showed that a vast majority of "continue" decisions were being made for sessions that had done nothing . A session is marked as "dead" if it has zero tool calls and two or fewer messages. The engine will not authorize a continuation from a dead session, as there is no state to carry forward.

### **The Boolean Decision Logic**

The final decision produced by the engine is a conjunction of these checks. Continuation is permitted only if the following conditions are met:

can_continue = NOT thrashing AND NOT blocker_halt AND NOT session_dead 
This structured approach ensures that the decision is auditable. Every call to check\_can\_continue is logged to the decision\_log table, including the reasons for the decision and the specific thresholds used at that moment .

## **Database Architecture and Information Flow**

The Carry Forward engine operates on a bifurcated database structure. It reads primary session data from the Hermes core database and stores its internal decision metadata in its own dedicated repository.

### **Interaction with state.db (Hermes Core)**

The state.db serves as the primary repository for all session and message history within the Hermes ecosystem. It uses an SQLite schema optimized for full-text search and session lineage tracking. Carry Forward accesses this database in a read-only capacity to extract the metrics needed for its continuation logic.

| Table Name | Description and Use Case for Carry Forward |
| :---- | :---- |
| sessions | Tracks session\_id, parent\_session\_id, message\_count, and tool\_call\_count. |
| messages | Contains the actual content and metadata of each message in a session. |
| messages\_fts | Enables fast search for specific keywords or patterns across the entire history. |

The relationship mapping is critical for reconstructing session chains. The parent\_session\_id column creates a foreign key relationship that allows the engine to find all ancestors or descendants of a given session using recursive common table expressions (CTEs).

### **Metadata Persistence in carry\_forward.db**

The carry\_forward.db repository is where the engine's internal logic and learned behaviors are stored. It acts as the "brain" of the decision engine, allowing it to improve over time.

| Table Name | Primary Purpose |
| :---- | :---- |
| decision\_log | Logs every continuation check, the decision, and the underlying reasoning. |
| decision\_outcomes | Logs what actually happened after a decision (e.g., was it productive?). |
| config | Stores the 8 tunable thresholds, including defaults and manual overrides. |
| chain\_meta | Stores high-level metadata about the lineage and root of a task chain. |
| chain\_git\_heads | Snapshots of the Git state at various points in the session chain. |
| blockers | A persistent list of active and resolved blockers with timestamps. |
| learned\_patterns | Stores statistical correlations between session features and outcomes. |

The link between decision\_log and decision\_outcomes is the cornerstone of the engine's feedback loop. Every decision is eventually paired with an outcome, providing the data necessary for threshold calibration .

## **The Self-Tuning Feedback Loop**

One of the most distinctive features of the Carry Forward engine is its ability to optimize its own performance. It does not utilize self-modifying code, which can be dangerous and unpredictable. Instead, it employs a "self-tuning behavior" loop that adjusts its decision thresholds based on empirical evidence .

### **Step 1: Decision Logging**

Whenever check\_can\_continue() is invoked, the engine records the decision (continue or halt) along with the reasons and the thresholds that were active at the time. This data is written to the decision\_log.

### **Step 2: Session Execution**

The agent then acts on the decision. If a continuation is authorized, the session plays out. If a halt is triggered, the loop terminates.

### **Step 3: Outcome Recording**

After a session completes, the record\_outcome command is called (often automatically via the context command). It inspects the actual results of the session—such as the number of tool calls and whether the Git HEAD moved—and records them in the decision\_outcomes table. A session is marked as "productive" if it has at least one tool call .

### **Step 4: Threshold Calibration**

The calibrate command performs a sweep of the threshold values against the historical data in the decision\_log and decision\_outcomes tables. The algorithm identifies the set of thresholds that would have maximized the system's accuracy across all past decisions. The primary metric for optimization is the F1 score (F1), which balances the need for precision and recall.

The F1 score is calculated as:

F1 = 2 * (Precision * Recall) / (Precision + Recall) 
Where precision is defined as the ratio of correctly predicted continuations to all predicted continuations, and recall is the ratio of correctly predicted continuations to all actually productive sessions. The engine then writes these optimal thresholds to the config table, marking the source as "calibration" .

### **Step 5: Validation via the Replay Harness**

To ensure that changes to the decision logic or thresholds improve the system, the replay\_harness.py provides a testing environment that evaluates proposed changes against historical data. This process allows developers to verify if a new rule increases the F1 score or if it introduces regressions. The harness flags anomalies, such as bulk backfills of data that might skew metrics, ensuring that the calibration process remains grounded in high-quality telemetry .

## **Command Reference and Operational Procedures**

The Carry Forward engine provides a robust CLI interface for managing session state, blockers, and configuration. These commands are intended for use by both the autonomous agent and human developers.

### **Core Interface Commands**

The engine provides three primary ways to read and act on its state:

1. **should-continue**: This is the primary interface for scripts and cron jobs. It returns a shell exit code (0 for "Go", 1 for "Halt"), allowing for simple integration into autonomous loops . 
2. **context**: This command generates a full summary of the current session chain. It includes a recap of what has happened, what needs to be done next, and whether a continuation is authorized. This output is designed to be passed to the next agent in a handoff. 
3. **check-can-continue**: This command provides a JSON representation of the full decision logic, including the status of all five checks and the specific thresholds applied.

### **Recording and Snapshotting**

To track progress accurately, the engine requires periodic snapshots of the environment's state:

* **record-git-heads SESSION\_ID**: This command snapshots the current Git HEADs for all projects associated with the session. This is the data used by Check 2 in the pipeline to determine if the agent is making progress . 
* **record-outcome**: This command manualizes the recording of what actually happened during a session. It is often triggered automatically but can be called manually to ensure the decision\_outcomes table is up to date.

### **Managing System Tuning**

Tuning the engine involves analyzing history and applying new thresholds:

* **calibrate**: As described, this automatically finds the optimal thresholds from historical decision/outcome pairs. It requires at least 10 outcomes to produce reliable results . 
* **learn**: This command mines the session history for broader patterns, such as the relationship between session size and productivity or time-of-day effects on performance. 
* **show-config**: This displays the current values for all 8 thresholds and indicates whether they were set by default, manual override, or calibration.

### **Threshold Configuration Reference**

The following table details the tunable parameters that govern the Carry Forward decision-making process.

| Key | Default | Functional Control |
| :---- | :---- | :---- |
| dead\_session\_threshold | 3 | Number of dead sessions in the lookback to trigger a halt. |
| dead\_lookback | 5 | The window of recent sessions to inspect for dead count. |
| orphan\_child\_threshold | 10 | Max dead children allowed before halting a runaway loop. |
| continuation\_rate\_min | 15 | Threshold below which source continuation rates trigger warnings. |
| blocker\_halt\_hours | 4 | Age of a blocker before it triggers a mandatory halt. |
| git\_min\_sessions | 3 | Number of sessions without Git movement required to trigger a halt. |
| parent\_size\_warning | 200 | Message count in the parent session above which a warning is issued. |
| chain\_depth\_warning | 8 | The number of continuations in a chain that triggers a depth warning. |

All thresholds can be manually adjusted using the set\_threshold command, which records the source as "manual" to prevent it from being automatically overwritten by the calibration process .

## **Integration with the Hermes Agent Ecosystem**

Carry Forward is built to complement the Hermes Agent, a self-improving AI assistant designed for deep terminal integration and persistent memory. While the agent handles the task execution, Carry Forward provides the logical framework for session persistence.

### **Persistent Memory and Session Resumption**

The Hermes Agent utilizes a "frozen snapshot" pattern for its persistent memory. Facts stored in MEMORY.md and user preferences in USER.md are injected into the system prompt at the beginning of a session. This ensures that the agent's knowledge of the environment and the user is stable throughout the session.

When Carry Forward decides to continue a session, it triggers the Hermes resumption logic. Resuming a session restores the full conversation history from the state.db. The agent is presented with a "Previous Conversation" panel that recaps past messages and tool calls, allowing it to pick up exactly where it left off. Carry Forward's context command enhances this by providing a structured handoff that emphasizes the most recent progress and upcoming goals.

### **The Role of Skills and Tools**

The Hermes ecosystem distinguishes between "Skills" and "Tools." Skills are high-level, instruction-based capabilities that the agent learns from experience, such as how to manage a Docker environment or navigate a specific codebase. Tools are lower-level integrations, such as a terminal emulator or a web search function.

Carry Forward's productivity metric—defined as having at least one tool call—directly aligns with this architecture. A session that utilizes tools is considered "productive" because it has interacted with the external world to gather information or effect change . This behavioral proxy is effective because it distinguishes between "thinking" (generating text) and "acting" (executing tool calls).

### **Managing Long-Running Workflows**

For long-running workflows that exceed the context window of a single session, Carry Forward facilitates the split and continuation process. The agent can use "proactive state externalization" by persisting intermediate results to files, creating durable checkpoints that Carry Forward uses to validate progress. This approach mitigates "context window anxiety" by allowing the agent to work in focused, manageable bursts while the continuation engine ensures the overall thread remains productive.

## **Comparative Analysis with Parallel Frameworks**

The Carry Forward engine represents a specific philosophy of agentic control that can be contrasted with other frameworks in the AI industry.

### **Contrast with Workflow Orchestrators**

Unlike frameworks such as OpenCode or LEA, Carry Forward is not a workflow orchestrator. OpenCode uses a "Spec Kit" and a team of specialized agents routed by a gate system to maintain project history. LEA uses multi-agent orchestration to adapt pedagogical scaffolding based on learner states.

In contrast, Carry Forward remains agnostic to the *content* of the task. It does not try to understand the code being written or the lesson being taught. It focuses entirely on the *operational health* of the computational loop. This separation of concerns allows Carry Forward to be more robust; it is a "dumb" engine that relies on hard metrics rather than the model's interpretation of its own success .

### **Comparison with Cognitive Memory Systems**

Systems like Mem0 and Honcho focus on long-term personalization and knowledge retention. They capture facts and entities to build a model of the user. Carry Forward operates at a different layer of the stack. While memory systems ensure the agent knows *what* it is doing, Carry Forward ensures the agent is actually *doing* it.

The following table compares Carry Forward with the cognitive memory features of the OpenCode framework.

| Feature | Carry Forward | OpenCode Framework |
| :---- | :---- | :---- |
| **Focus** | Operational Persistence | Cognitive Memory |
| **Primary Metric** | Tool Call Frequency | Positive Validation of Logic |
| **Memory Lifespan** | Session Chain (Episodic) | Long-term (Decay-based) |
| **Safety Mechanism** | Threshold-based Halt | Conflict Resolution Gate |
| **Learning Method** | Threshold Calibration | Memory Promotion/Demotion |

OpenCode uses a "Prediction Error" gating system to decide whether to create, update, or supersede a memory. Carry Forward uses a similar logic for its thresholds, where the calibrate command decides whether to update the system's "understanding" of what a productive session looks like.

## **Safety, Security, and Operational Guardrails**

Deploying autonomous agents requires stringent safety protocols to prevent unintended actions and runaway costs. Carry Forward integrates several guardrails into its decision logic and operational interface.

### **Human-in-the-Loop and Blocker States**

The blocker system is the primary mechanism for human oversight. By calling python3 carry\_forward.py block "reason", a user can pause an autonomous loop indefinitely. The engine will refuse to authorize any further continuations until the corresponding unblock command is issued . This is particularly useful when the agent encounters a security challenge or a decision that requires human ethical judgment.

### **Security-Hardened Code Practices**

The Carry Forward engine and the Hermes core are developed with a focus on security hardening. This includes preventing shell injection by using shlex.quote() when interpolating user input and resolving symlinks with os.path.realpath() to enforce access control checks. Furthermore, the system is designed to catch broad exceptions around tool execution, ensuring that a single failure does not crash the entire agent loop.

### **Platform-Specific Constraints**

The engine maintains cross-platform compatibility, acknowledging that Unix and Windows systems handle process management and signal handling differently. For example, the engine catches ImportError and NotImplementedError when dealing with Unix-only modules like termios or fcntl, providing fallback mechanisms for Windows users. This ensures that the Carry Forward logic remains consistent regardless of the underlying operating system.

## **Performance Analysis and Common Pitfalls**

The efficacy of the Carry Forward engine depends on the quality of the data it collects and the thresholds it uses. Several known pitfalls can affect its accuracy.

### **The Problem of "Lazy" Outcome Recording**

A recurring issue in agentic environments is that outcomes are not always recorded immediately. The record\_outcome command is often triggered when context is called, but if an agent halts without calling these, the data for that session is lost. This leads to an incomplete dataset for calibration, which can result in suboptimal thresholds .

### **Over-fitting to Historical Decisions**

The replay\_harness is a powerful tool for tuning, but it carries the risk of over-fitting. If thresholds are tuned to perfectly match a specific set of 500 historical sessions, the engine may become brittle and fail to generalize to new types of tasks. The system encourages the use of simple, robust rules—such as the dead-session check—over complex, multi-factor rules that are difficult to audit .

### **Data Skew from Bulk Backfills**

Telemetric data often contains anomalies that can distort statistical analysis. In the history of Carry Forward, a bulk backfill of 188 decisions at a single timestamp (1775844222) was identified as a major source of skew. These decisions were mostly "halts" on productive sessions, which artificially tanked the system's precision and recall metrics. The replay\_harness.py includes logic to flag and exclude such outliers to ensure the calibration remains accurate .

## **Future Directions: Toward Deeper Productivity Metrics**

The current definition of a "productive" session—one having at least one tool call—is a functional but blunt instrument. As autonomous agents become more sophisticated, the Carry Forward engine is expected to evolve to include more nuanced proxies for progress.

### **Integration with CI/CD and Testing Frameworks**

Future iterations of the engine could integrate feedback from continuous integration systems. A session that results in a successful test pass or an improved code coverage metric could be weighted more heavily in the "productive" category than one that merely makes a tool call. This would allow Carry Forward to distinguish between "exploratory" tool usage and "effective" tool usage.

### **Semantic Analysis of Session Trajectories**

By leveraging the full-text search capabilities of the state.db, the engine could analyze the semantic trajectory of a session chain. It could identify when an agent is "circling" a problem by searching for repetitive questions or commands in the message history. This would provide a more granular detection of thrashing that goes beyond simple message and tool call counts.

### **Cognitive Load and Context Pressure**

The system could also be expanded to monitor the "context pressure" of a session. As the context window fills up, model performance often degrades, leading to more errors and hallucinations. Carry Forward could use "context pressure" as a signal to proactively split a session, even if progress is being made, to ensure the agent always has sufficient "headroom" for clear reasoning.

## **Technical Summary of Commands and Database Schema**

For developers and systems architects, the following tables provide a quick reference for the Carry Forward engine's operational data and interface.

### **command Line Reference**

| Command | Category | Purpose |
| :---- | :---- | :---- |
| should-continue | Decision | Returns 0/1 exit code for autonomous loops. |
| context | State | Prints full chain summary and handoff context. |
| check-can-continue | Debug | Returns JSON decision logic for a specific session. |
| record-git-heads | Recording | Snapshots current Git state for progress tracking. |
| record-outcome | Recording | Logs the results of a session to the outcomes table. |
| calibrate | Tuning | Optimizes thresholds based on historical accuracy (F1). |
| learn | Tuning | Mines history for source rates and behavioral patterns. |
| show-config | Tuning | Displays current threshold values and their origin. |
| blockers | Management | Lists all currently active system blockers. |
| block "reason" | Management | Adds a blocker to stop further session continuations. |
| unblock "reason" | Management | Removes a blocker to allow the loop to resume. |

### **carry\_forward.db Schema and Relationships**

The internal database is structured around the lifecycle of a decision.

* **decision\_log.id** → **decision\_outcomes.decision\_id** (1:1 Relationship): Every decision check is eventually mapped to its actual outcome. 
* **sessions.id** (from state.db) → **decision\_log.session\_id** (Many:1 Relationship): Multiple checks might be performed for a single session during its lifecycle. 
* **sessions.parent\_session\_id** → **sessions.id** (Self-referential Relationship): This defines the session chain used for thrash and Git progress detection.

## **Conclusion: The Role of Discrete Decisions in Agentic Stability**

The Carry Forward engine addresses one of the most fundamental challenges in autonomous agent design: the requirement for a stable, auditable, and self-improving mechanism for session persistence. By moving the decision to continue or halt outside of the language model's immediate context and into a structured, metric-driven engine, Carry Forward provides a level of operational reliability that is unattainable through prompting alone.

Its unique "Check-Record-Calibrate" loop ensures that the system is not static but evolves alongside the agent's tasks and environment. Through its five-stage decision pipeline—covering thrashing, Git progress, learned patterns, blocker states, and immediate activity—the engine provides a comprehensive defense against the failure modes that typically derail autonomous loops.

As the industry transitions from simple assistants to complex, multi-day autonomous agents, the principles embodied by Carry Forward—discrete decision layers, empirical progress proxies, and recursive threshold optimization—will become essential components of the AI infrastructure. The Carry Forward engine stands as a pioneering example of how to build "cognitive stability" into autonomous systems, ensuring that they remain on task, within budget, and productive over extended computational horizons.

#### **Works cited**

1. Agentic Patterns Snippets, accessed April 10, 2026, [https://esc5221.github.io/awesome-agentic-patterns/](https://esc5221.github.io/awesome-agentic-patterns/) 
2. Session Storage | Hermes Agent, accessed April 10, 2026, [https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage](https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage) 
3. Sessions | Hermes Agent \- nous research, accessed April 10, 2026, [https://hermes-agent.nousresearch.com/docs/user-guide/sessions](https://hermes-agent.nousresearch.com/docs/user-guide/sessions) 
4. Load Factor Optimization AI Agent for Premium & Pricing in Insurance \- Insurnest, accessed April 10, 2026, [https://insurnest.com/agent-details/insurance/premium-pricing/load-factor-optimization-ai-agent-for-premium-&-pricing-in-insurance](https://insurnest.com/agent-details/insurance/premium-pricing/load-factor-optimization-ai-agent-for-premium-&-pricing-in-insurance) 
5. CLI Interface | Hermes Agent \- nous research, accessed April 10, 2026, [https://hermes-agent.nousresearch.com/docs/user-guide/cli](https://hermes-agent.nousresearch.com/docs/user-guide/cli) 
6. NousResearch/hermes-agent: The agent that grows with you \- GitHub, accessed April 10, 2026, [https://github.com/nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent) 
7. Persistent Memory | Hermes Agent, accessed April 10, 2026, [https://hermes-agent.nousresearch.com/docs/user-guide/features/memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) 
8. How Hermes Agent Memory Actually Works (And How to Make It Better) \- Vectorize, accessed April 10, 2026, [https://vectorize.io/articles/hermes-agent-memory-explained](https://vectorize.io/articles/hermes-agent-memory-explained) 
9. hermes-agent/CONTRIBUTING.md at main · NousResearch/hermes ..., accessed April 10, 2026, [https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md) 
10. opencode--spec-kit-skilled-agent-orchestration/README.md at main \- GitHub, accessed April 10, 2026, [https://github.com/MichelKerkmeester/opencode-spec-kit-framework/blob/main/README.md](https://github.com/MichelKerkmeester/opencode-spec-kit-framework/blob/main/README.md) 
11. A Multi-Agent AI Framework for Adaptive and Personalized Learning with Simulated Student Ag \- SciTePress, accessed April 10, 2026, [http://www.scitepress.org/Papers/2026/144189/144189.pdf](http://www.scitepress.org/Papers/2026/144189/144189.pdf) 
12. Short-Term vs Long-Term Memory in AI: How engineers design, evaluate, and scale memory systems \- Mem0, accessed April 10, 2026, [https://mem0.ai/blog/short-term-vs-long-term-memory-in-ai](https://mem0.ai/blog/short-term-vs-long-term-memory-in-ai)





