# Cloud-only operating policy

The owner clarified on 2026-09-20 that the hub operates in Google Cloud and that costs must be kept to the necessary minimum. The personal PC is a development interface, not a production worker or monitoring target.

The next requested milestone is hosted Cloud Qwen. The selected initial connection is Alibaba Model Studio Singapore `qwen3.7-flash-2026-07-15`, called by the Google Cloud controller. Its initial application policy is one request at a time, 128 completion tokens, thinking/search/tools disabled, no automatic retries, at most $0.01 reserved per call and $0.10 charged/reserved per UTC day including unresolved older reservations. These are application controls, not Alibaba account-wide billing caps. The Qwen connection remains disabled until the owner creates the provider account and supplies a region-matched pay-as-you-go key privately.

Initial deployment uses two request-billed Cloud Run services: controller and read-only dashboard. Each has minimum instances 0, maximum instances 1, 1 vCPU, and 512 MiB memory. No always-on worker VM, GPU, model training, local-model download, scheduled research loop, or bulk paid benchmark is enabled by this deployment. Browser polling pauses when hidden. Provider calls require bounded tasks; upgrades do not remove per-task limits.

Firestore stores durable controller records. Private Artifact Registry and a source bucket hold the small application build. A named database and separate service identities isolate RunCrew application access within the existing billed project. These resources can incur usage/storage charges; scale-to-zero is not a claim that the service is free or a billing hard cap.

Self-hosted inference means a separately provisioned Google Cloud inference service. It remains disabled until a measured task need and a cost-conscious execution plan justify it. Development modules can validate budgets and experiments without making model calls. Research findings never initiate infrastructure purchase or deployment by themselves.

Current deployment target is `project-0c6d31fa-509e-4116-a2c`, selected after Google refused to link billing to the new `runcrew-hub-20260920` project. A retry after the reported account upgrade returned the same billing-project quota error. The new unbilled project has no running services. Existing applications are not modified.
