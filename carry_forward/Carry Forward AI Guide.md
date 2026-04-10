# **Engineering Autonomous Persistence: A Technical Analysis of the Carry Forward Decision Engine**

The landscape of artificial intelligence has undergone a fundamental shift from reactive, prompt-based interactions toward proactive, goal-oriented agentic workflows. As autonomous agents are increasingly tasked with complex, multi-step operations such as software development, research, and system administration, the challenge of maintaining continuity across discrete computational sessions has become a primary bottleneck. The Carry Forward engine emerges as a specialized architectural solution to this problem, serving as a dedicated decision layer that governs session persistence. Unlike traditional task managers or context summarizers, Carry Forward focuses exclusively on the binary determination of whether an autonomous loop should spawn a subsequent session or halt operations based on empirical evidence of progress and system health \[User Query\].

This report examines the Carry Forward engine as a critical component of the Hermes Agent ecosystem developed by Nous Research. It provides an exhaustive analysis of the system's decision pipeline, its underlying data structures, and the recursive feedback loops that enable self-tuning behavior. By integrating version control progress, session activity metrics, and historical success patterns, Carry Forward addresses the "infinite loop" problem and "context window anxiety" that often plague autonomous agents.1 The following analysis explores the technical mechanisms through which Carry Forward achieves operational stability and optimizes resource allocation in agentic environments.

## **The Architecture of Session Continuation**

The primary function of the Carry Forward engine is to act as a governor for autonomous loops. When an agent concludes a session, the system must decide if the task is complete, if it has stalled, or if further work is required. This decision is not made by the language model itself, which is prone to hallucinations regarding its own progress, but by an external engine that evaluates the session's telemetry. The engine is explicitly defined not as a workflow orchestrator or a task manager, but as a decision engine for session continuation \[User Query\].

### **The Core Logic of the Decision Pipeline**

The decision to continue a session is the result of a five-stage pipeline. Each stage is designed to identify a specific failure mode or a signal of productivity. The logical flow ensures that a "Go" signal is only issued if all checks pass, thereby preventing unproductive cycles that consume compute tokens without making tangible progress.

#### **Stage 1: Dead Session Thrash Detection**

The first check in the pipeline addresses the phenomenon of "thrashing," where an agent enters a state of repeated, empty sessions. This often occurs due to configuration errors or the agent becoming stuck in a logic loop. The engine analyzes the lineage of the current session by traversing the parent\_session\_id chain in the state.db database.2

The thrash detection logic utilizes two key thresholds: dead\_session\_threshold and dead\_lookback. By default, if three out of the last five sessions in the chain contain zero messages and zero tool calls, the engine identifies a thrash state and triggers a hard halt \[User Query\]. This prevents the agent from wasting resources when it is clear that the loop is no longer interacting with the environment or the user.

#### **Stage 2: Version Control and Git Progress Validation**

A significant innovation in the Carry Forward engine is the use of Git as a proxy for productivity. In many autonomous tasks, particularly software engineering, progress is represented by file modifications and commits. The engine snapshots the Git HEAD at the beginning of a session chain and compares it to the state at the end of the evaluation period \[User Query\].

If the Git HEAD has not moved across a sequence of sessions defined by the git\_min\_sessions threshold (defaulting to 3), the engine concludes that the agent is "busy but unproductive." This stage is nested within the thrash detection logic; a Git stall is treated as a form of thrashing, even if the agent is exchanging messages. This prevents "planning loops" where the agent discusses work indefinitely without ever applying changes to the codebase \[User Query\].

#### **Stage 3: Pattern Recognition and Source Analysis**

The engine maintains a learned\_patterns table that tracks the continuation success rates of different session sources. For instance, an agent might discover that sessions initiated via the CLI have a ![][image1] success rate, while those from a web interface might be higher.4 If the current session belongs to a source with a historical continuation rate below the continuation\_rate\_min (typically ![][image2]), the engine issues a warning.

In addition to source rates, this stage evaluates "size effects." It has been observed that parent sessions with massive message counts (exceeding the parent\_size\_warning threshold, often set to 200\) tend to produce "dead" continuations due to context saturation and model amnesia.1 These patterns serve as guardrails rather than hard halts, informing the final decision output without necessarily blocking it.

#### **Stage 4: Persistent Blocker Management**

Autonomous agents frequently encounter blockers that require human intervention or external events to resolve, such as waiting for an API key or an ops team response. The Carry Forward engine allows these blockers to be logged with timestamps. The blocker\_halt\_hours threshold (default 4 hours) determines when a stale blocker should trigger a hard halt \[User Query\]. This prevents the agent from repeatedly attempting a task that is fundamentally blocked by an external dependency.

#### **Stage 5: Session Activity Verification**

The final check in the pipeline evaluates the immediate activity of the session currently being analyzed. This check was introduced in version 5.1 after data showed that a vast majority of "continue" decisions were being made for sessions that had done nothing \[User Query\]. A session is marked as "dead" if it has zero tool calls and two or fewer messages. The engine will not authorize a continuation from a dead session, as there is no state to carry forward.

### **The Boolean Decision Logic**

The final decision produced by the engine is a conjunction of these checks. Continuation is permitted only if the following conditions are met:

![][image3]  
This structured approach ensures that the decision is auditable. Every call to check\_can\_continue is logged to the decision\_log table, including the reasons for the decision and the specific thresholds used at that moment \[User Query\].

## **Database Architecture and Information Flow**

The Carry Forward engine operates on a bifurcated database structure. It reads primary session data from the Hermes core database and stores its internal decision metadata in its own dedicated repository.

### **Interaction with state.db (Hermes Core)**

The state.db serves as the primary repository for all session and message history within the Hermes ecosystem. It uses an SQLite schema optimized for full-text search and session lineage tracking.2 Carry Forward accesses this database in a read-only capacity to extract the metrics needed for its continuation logic.

| Table Name | Description and Use Case for Carry Forward |
| :---- | :---- |
| sessions | Tracks session\_id, parent\_session\_id, message\_count, and tool\_call\_count. |
| messages | Contains the actual content and metadata of each message in a session. |
| messages\_fts | Enables fast search for specific keywords or patterns across the entire history. |

The relationship mapping is critical for reconstructing session chains. The parent\_session\_id column creates a foreign key relationship that allows the engine to find all ancestors or descendants of a given session using recursive common table expressions (CTEs).2

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

The link between decision\_log and decision\_outcomes is the cornerstone of the engine's feedback loop. Every decision is eventually paired with an outcome, providing the data necessary for threshold calibration \[User Query\].

## **The Self-Tuning Feedback Loop**

One of the most distinctive features of the Carry Forward engine is its ability to optimize its own performance. It does not utilize self-modifying code, which can be dangerous and unpredictable. Instead, it employs a "self-tuning behavior" loop that adjusts its decision thresholds based on empirical evidence \[User Query\].

### **Step 1: Decision Logging**

Whenever check\_can\_continue() is invoked, the engine records the decision (continue or halt) along with the reasons and the thresholds that were active at the time. This data is written to the decision\_log.

### **Step 2: Session Execution**

The agent then acts on the decision. If a continuation is authorized, the session plays out. If a halt is triggered, the loop terminates.

### **Step 3: Outcome Recording**

After a session completes, the record\_outcome command is called (often automatically via the context command). It inspects the actual results of the session—such as the number of tool calls and whether the Git HEAD moved—and records them in the decision\_outcomes table. A session is marked as "productive" if it has at least one tool call \[User Query\].

### **Step 4: Threshold Calibration**

The calibrate command performs a sweep of the threshold values against the historical data in the decision\_log and decision\_outcomes tables. The algorithm identifies the set of thresholds that would have maximized the system's accuracy across all past decisions. The primary metric for optimization is the F1 score (![][image4]), which balances the need for precision and recall.

The ![][image4] score is calculated as:

![][image5]  
Where precision is defined as the ratio of correctly predicted continuations to all predicted continuations, and recall is the ratio of correctly predicted continuations to all actually productive sessions. The engine then writes these optimal thresholds to the config table, marking the source as "calibration" \[User Query\].

### **Step 5: Validation via the Replay Harness**

To ensure that changes to the decision logic or thresholds improve the system, the replay\_harness.py provides a testing environment that evaluates proposed changes against historical data. This process allows developers to verify if a new rule increases the ![][image4] score or if it introduces regressions. The harness flags anomalies, such as bulk backfills of data that might skew metrics, ensuring that the calibration process remains grounded in high-quality telemetry \[User Query\].

## **Command Reference and Operational Procedures**

The Carry Forward engine provides a robust CLI interface for managing session state, blockers, and configuration. These commands are intended for use by both the autonomous agent and human developers.

### **Core Interface Commands**

The engine provides three primary ways to read and act on its state:

1. **should-continue**: This is the primary interface for scripts and cron jobs. It returns a shell exit code (0 for "Go", 1 for "Halt"), allowing for simple integration into autonomous loops \[User Query\].  
2. **context**: This command generates a full summary of the current session chain. It includes a recap of what has happened, what needs to be done next, and whether a continuation is authorized. This output is designed to be passed to the next agent in a handoff.5  
3. **check-can-continue**: This command provides a JSON representation of the full decision logic, including the status of all five checks and the specific thresholds applied.

### **Recording and Snapshotting**

To track progress accurately, the engine requires periodic snapshots of the environment's state:

* **record-git-heads SESSION\_ID**: This command snapshots the current Git HEADs for all projects associated with the session. This is the data used by Check 2 in the pipeline to determine if the agent is making progress \[User Query\].  
* **record-outcome**: This command manualizes the recording of what actually happened during a session. It is often triggered automatically but can be called manually to ensure the decision\_outcomes table is up to date.

### **Managing System Tuning**

Tuning the engine involves analyzing history and applying new thresholds:

* **calibrate**: As described, this automatically finds the optimal thresholds from historical decision/outcome pairs. It requires at least 10 outcomes to produce reliable results \[User Query\].  
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

All thresholds can be manually adjusted using the set\_threshold command, which records the source as "manual" to prevent it from being automatically overwritten by the calibration process \[User Query\].

## **Integration with the Hermes Agent Ecosystem**

Carry Forward is built to complement the Hermes Agent, a self-improving AI assistant designed for deep terminal integration and persistent memory.5 While the agent handles the task execution, Carry Forward provides the logical framework for session persistence.

### **Persistent Memory and Session Resumption**

The Hermes Agent utilizes a "frozen snapshot" pattern for its persistent memory. Facts stored in MEMORY.md and user preferences in USER.md are injected into the system prompt at the beginning of a session.7 This ensures that the agent's knowledge of the environment and the user is stable throughout the session.

When Carry Forward decides to continue a session, it triggers the Hermes resumption logic. Resuming a session restores the full conversation history from the state.db. The agent is presented with a "Previous Conversation" panel that recaps past messages and tool calls, allowing it to pick up exactly where it left off.3 Carry Forward's context command enhances this by providing a structured handoff that emphasizes the most recent progress and upcoming goals.

### **The Role of Skills and Tools**

The Hermes ecosystem distinguishes between "Skills" and "Tools." Skills are high-level, instruction-based capabilities that the agent learns from experience, such as how to manage a Docker environment or navigate a specific codebase.9 Tools are lower-level integrations, such as a terminal emulator or a web search function.

Carry Forward's productivity metric—defined as having at least one tool call—directly aligns with this architecture. A session that utilizes tools is considered "productive" because it has interacted with the external world to gather information or effect change \[User Query\]. This behavioral proxy is effective because it distinguishes between "thinking" (generating text) and "acting" (executing tool calls).

### **Managing Long-Running Workflows**

For long-running workflows that exceed the context window of a single session, Carry Forward facilitates the split and continuation process. The agent can use "proactive state externalization" by persisting intermediate results to files, creating durable checkpoints that Carry Forward uses to validate progress.1 This approach mitigates "context window anxiety" by allowing the agent to work in focused, manageable bursts while the continuation engine ensures the overall thread remains productive.

## **Comparative Analysis with Parallel Frameworks**

The Carry Forward engine represents a specific philosophy of agentic control that can be contrasted with other frameworks in the AI industry.

### **Contrast with Workflow Orchestrators**

Unlike frameworks such as OpenCode or LEA, Carry Forward is not a workflow orchestrator. OpenCode uses a "Spec Kit" and a team of specialized agents routed by a gate system to maintain project history.10 LEA uses multi-agent orchestration to adapt pedagogical scaffolding based on learner states.11

In contrast, Carry Forward remains agnostic to the *content* of the task. It does not try to understand the code being written or the lesson being taught. It focuses entirely on the *operational health* of the computational loop. This separation of concerns allows Carry Forward to be more robust; it is a "dumb" engine that relies on hard metrics rather than the model's interpretation of its own success \[User Query\].

### **Comparison with Cognitive Memory Systems**

Systems like Mem0 and Honcho focus on long-term personalization and knowledge retention.8 They capture facts and entities to build a model of the user. Carry Forward operates at a different layer of the stack. While memory systems ensure the agent knows *what* it is doing, Carry Forward ensures the agent is actually *doing* it.

The following table compares Carry Forward with the cognitive memory features of the OpenCode framework.

| Feature | Carry Forward | OpenCode Framework |
| :---- | :---- | :---- |
| **Focus** | Operational Persistence | Cognitive Memory |
| **Primary Metric** | Tool Call Frequency | Positive Validation of Logic |
| **Memory Lifespan** | Session Chain (Episodic) | Long-term (Decay-based) |
| **Safety Mechanism** | Threshold-based Halt | Conflict Resolution Gate |
| **Learning Method** | Threshold Calibration | Memory Promotion/Demotion |

OpenCode uses a "Prediction Error" gating system to decide whether to create, update, or supersede a memory.10 Carry Forward uses a similar logic for its thresholds, where the calibrate command decides whether to update the system's "understanding" of what a productive session looks like.

## **Safety, Security, and Operational Guardrails**

Deploying autonomous agents requires stringent safety protocols to prevent unintended actions and runaway costs. Carry Forward integrates several guardrails into its decision logic and operational interface.

### **Human-in-the-Loop and Blocker States**

The blocker system is the primary mechanism for human oversight. By calling python3 carry\_forward.py block "reason", a user can pause an autonomous loop indefinitely. The engine will refuse to authorize any further continuations until the corresponding unblock command is issued \[User Query\]. This is particularly useful when the agent encounters a security challenge or a decision that requires human ethical judgment.

### **Security-Hardened Code Practices**

The Carry Forward engine and the Hermes core are developed with a focus on security hardening. This includes preventing shell injection by using shlex.quote() when interpolating user input and resolving symlinks with os.path.realpath() to enforce access control checks.9 Furthermore, the system is designed to catch broad exceptions around tool execution, ensuring that a single failure does not crash the entire agent loop.

### **Platform-Specific Constraints**

The engine maintains cross-platform compatibility, acknowledging that Unix and Windows systems handle process management and signal handling differently. For example, the engine catches ImportError and NotImplementedError when dealing with Unix-only modules like termios or fcntl, providing fallback mechanisms for Windows users.9 This ensures that the Carry Forward logic remains consistent regardless of the underlying operating system.

## **Performance Analysis and Common Pitfalls**

The efficacy of the Carry Forward engine depends on the quality of the data it collects and the thresholds it uses. Several known pitfalls can affect its accuracy.

### **The Problem of "Lazy" Outcome Recording**

A recurring issue in agentic environments is that outcomes are not always recorded immediately. The record\_outcome command is often triggered when context is called, but if an agent halts without calling these, the data for that session is lost. This leads to an incomplete dataset for calibration, which can result in suboptimal thresholds \[User Query\].

### **Over-fitting to Historical Decisions**

The replay\_harness is a powerful tool for tuning, but it carries the risk of over-fitting. If thresholds are tuned to perfectly match a specific set of 500 historical sessions, the engine may become brittle and fail to generalize to new types of tasks. The system encourages the use of simple, robust rules—such as the dead-session check—over complex, multi-factor rules that are difficult to audit \[User Query\].

### **Data Skew from Bulk Backfills**

Telemetric data often contains anomalies that can distort statistical analysis. In the history of Carry Forward, a bulk backfill of 188 decisions at a single timestamp (1775844222) was identified as a major source of skew. These decisions were mostly "halts" on productive sessions, which artificially tanked the system's precision and recall metrics. The replay\_harness.py includes logic to flag and exclude such outliers to ensure the calibration remains accurate \[User Query\].

## **Future Directions: Toward Deeper Productivity Metrics**

The current definition of a "productive" session—one having at least one tool call—is a functional but blunt instrument. As autonomous agents become more sophisticated, the Carry Forward engine is expected to evolve to include more nuanced proxies for progress.

### **Integration with CI/CD and Testing Frameworks**

Future iterations of the engine could integrate feedback from continuous integration systems. A session that results in a successful test pass or an improved code coverage metric could be weighted more heavily in the "productive" category than one that merely makes a tool call.1 This would allow Carry Forward to distinguish between "exploratory" tool usage and "effective" tool usage.

### **Semantic Analysis of Session Trajectories**

By leveraging the full-text search capabilities of the state.db, the engine could analyze the semantic trajectory of a session chain. It could identify when an agent is "circling" a problem by searching for repetitive questions or commands in the message history. This would provide a more granular detection of thrashing that goes beyond simple message and tool call counts.

### **Cognitive Load and Context Pressure**

The system could also be expanded to monitor the "context pressure" of a session. As the context window fills up, model performance often degrades, leading to more errors and hallucinations. Carry Forward could use "context pressure" as a signal to proactively split a session, even if progress is being made, to ensure the agent always has sufficient "headroom" for clear reasoning.10

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
| calibrate | Tuning | Optimizes thresholds based on historical accuracy (![][image4]). |
| learn | Tuning | Mines history for source rates and behavioral patterns. |
| show-config | Tuning | Displays current threshold values and their origin. |
| blockers | Management | Lists all currently active system blockers. |
| block "reason" | Management | Adds a blocker to stop further session continuations. |
| unblock "reason" | Management | Removes a blocker to allow the loop to resume. |

### **carry\_forward.db Schema and Relationships**

The internal database is structured around the lifecycle of a decision.

* **decision\_log.id** ![][image6] **decision\_outcomes.decision\_id** (1:1 Relationship): Every decision check is eventually mapped to its actual outcome.  
* **sessions.id** (from state.db) ![][image6] **decision\_log.session\_id** (Many:1 Relationship): Multiple checks might be performed for a single session during its lifecycle.  
* **sessions.parent\_session\_id** ![][image6] **sessions.id** (Self-referential Relationship): This defines the session chain used for thrash and Git progress detection.

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

[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACMAAAAXCAYAAACBMvbiAAACB0lEQVR4Xu2VO0gcURSGTzQ+ImqCYgq1sTSg4COQwiKVQQgo+GhSRGwkYGujndqIaKlgs4JBEAzYCEFFUkhQY0KIwcaA24iSLgQs0uj/7527c/Yws7sgVtkPPphz7pk7d+9rRf4T2mGdTRoaYbFNWl7YhKJM3IdabEPAB7ip4s/i+nsQxBVwBP6Gk74oCn6AHd1I+LJmAV7CffgHJmB9RoV7952Kv8J1mIQ78Bweihvkw7As5LG44mNxncUNZkPCDjhDrPsOq9MVLtev4jP1TB7BE9hs8mlKYDmslPjBcH0XxXXm4VSzdkLlGL9WMWs0c3Dc5CKpknAwdmN1B/k3Kvc+yHEpPHZmvqhn8gkWmVwk2QZD7Ongh1i7pnIz8CesCdTLsaSec5JtmaLwtfb0TcMreKRyPXBXxTnJNTMW1k3ZZAS18BQ2BTH31A/4Dc77IoseTOSxU3Bf8KjnM4OrcDR47oJ/4XNxP3jZF1nyHcwr+M8mYxiCH1XMe2xPxdwakaugB8PjbmmFv+BLleMG5QUWx7CJ2XfC5OyeS6EHU2raSBL2iRvoE9gGZ+FKWJLBW5sA1+KWTfPMxCk6JRwMP6rhLevbrPrS8zSIO+KWA7ilYu45XrgZ2A/QC9XODWvbvQOqjvBS24aDJk96xc3O0yAeU233AmdF/19FwT/lDonZvAUKFLgLtxqjdqcbFWWnAAAAAElFTkSuQmCC>

[image2]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACMAAAAXCAYAAACBMvbiAAACBElEQVR4Xu2VO0gdURCGxyca1CQ+0mihWClRMDFgkcYyEjAkaKvYiGAlWASr+GpUxCpio5AqRaJFFDEihkjwLfgqtNBGFFMKtuYfZzc7Zzx6DWKV+8EHO3Nm986es+dcov+EZzDPJg0FMMkmLVU2EVBu4gRYYXJf4ISKf5E8j2uZB7AJnsKOsMhHGcmDLii6WcP5HTgGe+AR3IeJpqZFxWvwMzyE3+EBXCJpMjkqi3hIUrxK8rCbmtFOwUdOheTfqZib1aTDLVhi8n9JgWkwg25uZhcOw6/wA7kzEsL3vlYxL4emD7abnJdMiprxfVjfbMKDnZkVdc3Mk/8lrnCbZgrhAByHqc6o0A23YXagXo6P6jomsZaJ159/qBe+hXvwqVMhdMETuKxyr+CsimMSa2Y2Sc6GkGP4G+aqnI8cku+tKIj5m+JnrZPMshfdjHfbGcZIajtN3vIJNgfXL+EZfEHywiNhkeVfm+knqZ2zA4p6OK1iPsd0PX8avlVwmuHtrqkJ8tUqNxTkJlXO0mhirh81Oe+Jr5uxO6UYvif3LX6Q1F53bjTYBDgnWTZNqYkvqaSomTdmjGkzMdctkH9J80l2nmWR3JnkXcsHrkPYhJb/ezR8gG3AQfiT5CR+7FQIfKjNwDo7AGpJZudJELeqsXuBZyXLJg38p/ycrvl448SJcxf+AOhBdNoAk4PCAAAAAElFTkSuQmCC>

[image3]: <data:image/png;base64,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>

[image4]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQAAAAaCAYAAAC3g3x9AAAA/klEQVR4Xu2UywpBURSGV0qSXMYkl5lHMFMGplIGkkdhoBi5PIEnYaBEKRl6BWVoJgP+3Tq7luVkH2fKV1+d/a9lnbPPBdHP04DLgAYiRzx0AR+wCWvCNlx7ta8YwIMOPeLwqkMXKzjVoWCjg0+YLZstpVVeF8dzcexkTO/3qAITKgvMjnhg33MCzy8dX5CEd+KB9vXYemtNDPZgShckM+If70UWgRexNmRhl7g3r2ovHImbRiKLwqpYW1rEvQVdsGSIG4zmSbtwDuwQN9yI748L8xX5btmcwV6ZdCibfLBXWFR5aOzAki6ExQ4s60IYavBEPNC8GYH/zv788eEJJlI8unzFwecAAAAASUVORK5CYII=>

[image5]: <data:image/png;base64,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>

[image6]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABMAAAAYCAYAAAAYl8YPAAAAdklEQVR4XmNgGAUjG4gCcRe6ILmAGYgfADEPmjjZoAmI89AFKQFaQHwQiJXRJRSB2IEMnA/Er4GYkwEJVADxbjLwHiD+D8Q+DBQCRiCeBcQT0CXIAdOBuBJdkBzgAcSb0AXJAUJAfBVdkFzABcS66IKjYBTgAQB/3xii8R6X5gAAAABJRU5ErkJggg==>