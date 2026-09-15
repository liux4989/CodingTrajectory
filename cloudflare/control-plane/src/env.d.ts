/* eslint-disable */
// Maintained by hand for the fact-based authority (no R2 artifact bucket).
interface __BaseEnv_Env {
	WORKER_VERSION: WorkerVersionMetadata;
	CT_PRINCIPALS: string;
	WORKSPACES: DurableObjectNamespace<import("./index").Workspace>;
}
declare namespace Cloudflare {
	interface GlobalProps {
		mainModule: typeof import("./index");
		durableNamespaces: "Workspace";
	}
	interface StagingEnv {
		WORKER_VERSION: WorkerVersionMetadata;
		CT_PRINCIPALS: string;
		WORKSPACES: DurableObjectNamespace<import("./index").Workspace>;
	}
	interface Env extends __BaseEnv_Env {}
}
interface Env extends __BaseEnv_Env {}
type StringifyValues<EnvType extends Record<string, unknown>> = {
	[Binding in keyof EnvType]: EnvType[Binding] extends string ? EnvType[Binding] : string;
};
declare namespace NodeJS {
	interface ProcessEnv extends StringifyValues<Pick<Cloudflare.Env, "CT_PRINCIPALS">> {}
}
