# RainCheck

RainCheck scrapes Yahoo Auctions Japan for new listings of tracked items and emails the best deals.
It benchmarks each listing against a market price computed from active eBay listings.
It is written in Python with a SQLite database.

RC-06 is merged and stores distinct eBay listing-price observations in schema version 2.

## Layout

- `src/schema.sql` defines the tables at schema version 2; `src/db.py` holds every database function and the versioned migrations.
- `src/yahoo_scrape.py` scrapes Yahoo and emails deals.
- `src/ebay_pricing.py` computes each item's market price from the eBay Browse API and stores it.
- `src/catalog.json` is seed input; jobs read tracked items from the database.
- `docs/BACKLOG.md` is the prioritized feature list; `docs/specs/` holds one approved spec per feature.
- `.claude/agents/` defines the planner, test-writer, implementer, reviewer, and verifier agents.
  `.codex/agents/` holds the same five ported for OpenAI Codex, and `AGENTS.md` is the Codex handoff; keep them in sync with these files.

## Guidance level

help_level: HIGH.
The user is learning and prioritizes quality over speed.
Explain what each step does and why, suggest better alternatives, and flag best practices and tradeoffs.

## Feature workflow

Every backlog item goes through these stages in order.
The main session acts as the orchestrator and only advances when a stage actually passes.

1. **Plan:** the `planner` agent writes `docs/specs/<ID>-<slug>.md`.
2. **Approve:** the user reads and approves the spec.
   Nothing is built from an unapproved spec.
3. **Test:** the `test-writer` agent writes failing tests from the spec.
4. **Implement:** the `implementer` agent makes the tests pass, running in an isolated git worktree on its own branch.
5. **Review:** the `reviewer` agent reviews the diff with fresh context.
   On CHANGES REQUESTED, the implementer fixes and the reviewer runs again.
6. **Verify:** the `verifier` agent runs the real entry points against a throwaway database.
7. **Merge:** the user reviews the pull request and merges it.
   The user should be able to explain every change before merging.

Parallelism rules:

- Features touching different files may run in parallel, each in its own worktree.
- Features touching `src/schema.sql`, `src/db.py`, or `src/catalog.json` run one at a time.
- Agent worktrees are created from `main`, not from the branch you are on, so they start without the feature's spec and tests.
  Commit the spec and tests on the feature branch, then tell the implementer that branch's name; its first step fast-forwards its worktree onto it.
  Uncommitted work is invisible to agents.

## Conventions

- Parameterized SQL only; single-value tuples need a trailing comma; every `UPDATE` and `DELETE` has a `WHERE`.
- Every network call has a timeout.
- Never commit `.env`, secrets, or `raincheck.db`.
- Tests never touch the network, send email, or use the real `raincheck.db`.
- Never use the em dash character; use a plain dash.
- In Markdown, put each full sentence on its own line.
- Commit messages never include a Co-Authored-By trailer or any AI attribution.

<!-- BEGIN AWS Agent Toolkit rules -->
# AWS Guidance for the new AWS experience

This user has signed up for the new AWS experience. This experience lets you sign into AWS using a social provider and requires the following additional context.

Where this guidance conflicts with the project's own instructions, the project's instructions take precedence.

## Context

### Terminology:

- Say "project" instead of "account" - a project contains an AWS account and settings for sharing with other collaborators
- Say "team member" instead of "IAM user" - users are invited by email, not created or federated in IAM
- Say "AWS Settings" when referring to management tasks at [settings.aws.com](https://settings.aws.com/) (project management, billing, team members, spend limits). Users view their actual AWS resources in the AWS Management Console.
- Say "selected Region" when referring to the user's Region - not "home Region"
- The user has a managed IAM experience. This includes a managed service control policies (SCP) and resource control policies (RCP) that govern the use of AWS. They will still need to use IAM to create policies to let services work with each other. If there are questions about the SCPs or RCPs, go to the documentation at https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html

### Constraints:

- All projects share a single AWS Region determined by the user's contact address. Resources cannot be created in other Regions
- When developing:
  - MUST create all Regional resources in the project's assigned Region
  - You CAN create AWS WAF and Cloudwatch Logs resources in us-east-1 when there are global resources (like a global WAF instance) that require a connection to dependencies in us-east-1. You should not use these for any other reason, because resources in the selected Region will provide lower cost (due to no cross-Region traffic), increased availability (due to no cross-Region traffic), and easier manageability (due to not needing to look in another Region). When you need to do an inventory of resources, you need to look in both the selected Region and us-east-1 for Cloudwatch Logs or WAF resources.
  - MUST NOT attempt to create Lambda, API Gateway, or other Regional resources in any other Region
  - MUST direct users to confirm their Region in AWS Settings > View all projects > Overview > Additional Info > Region. If the user cannot confirm their Region, check in ~/.aws/config
  - MUST NOT use Lambda@Edge - excluded from both Lambda and CloudFront
  - MUST NOT use CloudFormation StackSets - no multi-account or multi-Region deployments
  - MUST NOT attempt cross-Region actions - no cross-Region replication for DynamoDB/S3/RDS, no multi-Region KMS keys
  - MUST NOT use Route 53 cross-Region routing - geolocation, latency-based, and failover routing policies are not available
  - CloudFront is a global service and its actions ARE allowed in `us-east-1`. A user can create a CloudFront distribution pointing to their project-region Lambda function URL or API Gateway. However, Lambda and API Gateway themselves MUST NOT be created in `us-east-1` - they must be in the project Region.
  - Reduced availability in `eu-north-1` specifically: Amazon Rekognition, Amazon Textract, Amazon Personalize, AWS App Runner are not available in that Region.
- IAM permissions for human access are managed by AWS. Don't assign roles to team members unless absolutely necessary
- The user may have a spend limit if they are on the paid plan. The limit that pauses their project if it's exceeded. If resources suddenly become inaccessible, ask if they have a spend limit configured. Only project owners can modify a spend limit.
- When developing:
  - MUST ask about spend limit status if the user reports sudden "Access Denied" errors on operations that previously worked
  - MUST direct users to check spend status in AWS Settings > Billing
  - MUST check if a user has upgraded their account to the paid plan
  - MUST ask the user if they want to clean up the successfully created resources or keep them to reduce cost
- The user sets up billing, creates spend limits, and retrieves and pays invoices in AWS Settings. The user creates budgets and optimizes their costs in the AWS Billing and Cost Management console
- Not all AWS services are available. If a service isn't working, do the following:
  1. Run the command `aws freetier get-account-plan-state`
  2. If accountPlanType": "FREE", check the [Free Tier supported services list](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html#supported-services-free-tier) next,
  3. If accountPlanType": "PAID", check the [Paid Tier supported services list](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html#supported-services-paid-plan).
  4. If neither list shows the service, check the [Not supported for this experience list](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html#unsupported-services). The user will need to activate advanced features to access this service.
- Users can activate advanced AWS services and capabilities for their account.
- Before starting a task, check whether a relevant AWS skill is available. Load the skill with retrieve_skill and prefer its guidance over general knowledge.

### Help level

- help_level (required): LOW, MEDIUM, or HIGH. While a user is building, you MUST ask the user: "How much guidance would you like from me? Low (I only flag security risks), medium (I ask a couple of clarifying questions if something seems off), or high (I explain what I'm doing, suggest alternatives, and flag best practices)."

You CAN update this rule file to save a user's help_level.

Constraints for each level:

**LOW:**

- MUST follow all constraints in this context file
- MUST execute the user’s request without modification
- MUST NOT ask clarifying questions unless the action would create a security vulnerability
- MUST NOT suggest alternatives or improvements

**MEDIUM:**

- MUST execute the user's request
- MAY ask up to two clarifying questions per task if the request has an ambiguity or a potential issue
- MUST NOT repeat a question or suggestion the user has already dismissed
- MUST NOT explain trade-offs or alternatives unless the user asks

**HIGH:**

- MUST explain what each step does and why before executing it
- MUST suggest alternatives when a better approach exists
- MUST flag best practices and explain trade-offs
- MUST still execute the user's choice if they disagree with a suggestion
<!-- END AWS Agent Toolkit rules -->
