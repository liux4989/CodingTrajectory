/* eslint-disable */
// Maintained by hand for the artifact-backed authority.
interface __BaseEnv_Env {
	WORKER_VERSION: WorkerVersionMetadata;
	CT_PRINCIPALS: string;
	CT_CURSOR_KEY: string;
	CT_REPLACEMENT_WORKSPACE_ID?: string;
	CT_REPLACEMENT_EXPORT_SHA256?: string;
	WORKSPACES: DurableObjectNamespace<import("./index").Workspace>;
	ARTIFACTS: R2Bucket;
}
declare namespace Cloudflare {
	interface GlobalProps {
		mainModule: typeof import("./index");
		durableNamespaces: "Workspace";
	}
	interface StagingEnv {
		WORKER_VERSION: WorkerVersionMetadata;
		CT_PRINCIPALS: string;
		CT_CURSOR_KEY: string;
		WORKSPACES: DurableObjectNamespace<import("./index").Workspace>;
		ARTIFACTS: R2Bucket;
	}
	interface Env extends __BaseEnv_Env {}
}
interface Env extends __BaseEnv_Env {}
type StringifyValues<EnvType extends Record<string, unknown>> = {
	[Binding in keyof EnvType]: EnvType[Binding] extends string ? EnvType[Binding] : string;
};
declare namespace NodeJS {
	interface ProcessEnv extends StringifyValues<Pick<Cloudflare.Env, "CT_PRINCIPALS" | "CT_CURSOR_KEY">> {}
}
