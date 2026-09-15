/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type StrategyId = string;
export type Version = string;
export type Name = string;
export type Description = string;
export type Author = string;
export type Type = "deterministic";
export type Rule = string;
export type RuleVersion = string;
export type ScopeTypes = ("project" | "session")[];
export type InputWindow = string;
export type Severities = string[];
export type FindingPolicy = string;
export type Id = string;
export type Label = string;
export type Detail = string;
export type Permissions = StrategyPermission[];

/**
 * Versioned strategy definition shown to humans before configuration.
 */
export interface StrategyManifest {
  strategy_id: StrategyId;
  version: Version;
  name: Name;
  description: Description;
  author: Author;
  evaluator: EvaluatorSpec;
  scope_types: ScopeTypes;
  trigger: Trigger;
  input_window: InputWindow;
  measures: Measures;
  severities: Severities;
  finding_policy: FindingPolicy;
  permissions: Permissions;
  required_core_methods: RequiredCoreMethods;
  config_schema?: ConfigSchema;
}
/**
 * Evaluator provenance recorded on every evaluation.
 */
export interface EvaluatorSpec {
  type?: Type;
  rule?: Rule;
  rule_version?: RuleVersion;
}
export interface Trigger {
  [k: string]: string;
}
export interface Measures {
  [k: string]: string;
}
export interface StrategyPermission {
  id: Id;
  label: Label;
  detail: Detail;
}
export interface RequiredCoreMethods {
  [k: string]: number;
}
export interface ConfigSchema {
  [k: string]: string;
}
