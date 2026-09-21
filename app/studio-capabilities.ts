export type ReferenceContract = {
  media_types?: string[];
  min_count?: number;
  max_count?: number;
  ordered?: boolean;
  prompt_examples?: string[];
  order_field?: string;
  index_base?: number;
  prompt_reference_format?: string;
  prompt_index_base?: number;
  roles?: string[];
};

export type ProfileCapability = {
  id: string;
  version: string;
  display_name: string;
  output_type: "video" | "image";
  compiler: string;
  manifest_sha256: string;
  compatible_identities?: Array<{ version: string; manifest_sha256: string }>;
  sampling_mode?: "turbo4" | "base" | "default";
  input_modalities: string[];
  available: boolean;
  missing_nodes?: string[];
  missing_models?: string[];
  missing_model_files?: string[];
  missing_options?: string[];
  parameter_schema: Record<string, string>;
  defaults: Record<string, string | number>;
  limits: Record<string, number | [number, number]>;
  reference_contract?: ReferenceContract;
  license_id?: string;
  license_url?: string;
  use_notice?: string;
};

export type UnavailableProfileCapability = {
  id: string;
  version?: string | null;
  display_name: string;
  output_type: "video" | "image";
  input_modalities?: string[];
  available: false;
  selectable?: false;
  placeholder?: boolean;
  status?: string;
  reason?: string;
};

export type ImageReferencePolicy = {
  min: number;
  max: number;
  ordered: boolean;
  promptExamples: string[];
  indexBase: number;
  promptIndexBase: number;
  source: "capability" | "limits" | "legacy-adapter";
};

// Upper envelope for mixed H3 references. Individual image profiles remain
// constrained by their own advertised reference_contract/limits.
export const STUDIO_REFERENCE_BUDGET = 12;

function finiteCount(value: unknown): number | undefined {
  const count = Number(value);
  return Number.isFinite(count) && count >= 0 ? Math.floor(count) : undefined;
}

function rangeFromLimits(profile: ProfileCapability): [number, number] | undefined {
  for (const key of ["image_references", "reference_images", "reference_count"]) {
    const value = profile.limits[key];
    if (Array.isArray(value) && value.length === 2) {
      const min = finiteCount(value[0]);
      const max = finiteCount(value[1]);
      if (min !== undefined && max !== undefined) return [Math.min(min, max), Math.max(min, max)];
    }
    const max = finiteCount(value);
    if (max !== undefined) return [profile.input_modalities.includes("image") ? 1 : 0, max];
  }
  return undefined;
}

/**
 * Normalizes the evolving capabilities response into one frontend contract.
 * New profiles should publish reference_contract. The compiler checks below
 * exist only so older deployed backends keep working while they are upgraded.
 */
export function imageReferencePolicy(profile?: ProfileCapability): ImageReferencePolicy {
  if (!profile) return { min: 0, max: 0, ordered: true, promptExamples: [], indexBase: 0, promptIndexBase: 1, source: "legacy-adapter" };
  const contract = profile.reference_contract;
  if (contract && !(contract.media_types ?? ["image"]).includes("image")) {
    return { min: 0, max: 0, ordered: contract.ordered !== false, promptExamples: [], indexBase: finiteCount(contract.index_base) ?? 0, promptIndexBase: finiteCount(contract.prompt_index_base) ?? 1, source: "capability" };
  }
  if (contract) {
    const min = finiteCount(contract.min_count) ?? 0;
    const advertisedMax = finiteCount(contract.max_count) ?? (profile.input_modalities.includes("image") ? 1 : 0);
    return {
      min: Math.min(min, STUDIO_REFERENCE_BUDGET),
      max: Math.min(Math.max(min, advertisedMax), STUDIO_REFERENCE_BUDGET),
      ordered: contract.ordered !== false,
      promptExamples: (contract.prompt_examples ?? []).filter((item) => typeof item === "string" && item.trim()).slice(0, 3),
      indexBase: finiteCount(contract.index_base) ?? 0,
      promptIndexBase: finiteCount(contract.prompt_index_base) ?? 1,
      source: "capability",
    };
  }
  const limited = rangeFromLimits(profile);
  if (limited) return {
    min: Math.min(limited[0], STUDIO_REFERENCE_BUDGET),
    max: Math.min(limited[1], STUDIO_REFERENCE_BUDGET),
    ordered: true,
    promptExamples: [],
    indexBase: 0,
    promptIndexBase: 1,
    source: "limits",
  };
  if (!profile.input_modalities.includes("image")) return { min: 0, max: 0, ordered: true, promptExamples: [], indexBase: 0, promptIndexBase: 1, source: "legacy-adapter" };
  const compiler = profile.compiler.toLowerCase();
  const optionalMultiImage = compiler.includes("flux2") || compiler.includes("flux_2") || compiler.includes("klein");
  return {
    min: optionalMultiImage ? 0 : 1,
    max: optionalMultiImage ? Math.min(4, STUDIO_REFERENCE_BUDGET) : 1,
    ordered: true,
    promptExamples: [],
    indexBase: 0,
    promptIndexBase: 1,
    source: "legacy-adapter",
  };
}

export function imageProfileAcceptsReferenceCount(profile: ProfileCapability, count: number) {
  if (profile.output_type !== "image") return false;
  const policy = imageReferencePolicy(profile);
  return count >= policy.min && count <= policy.max;
}

export function profileSupportsParameter(profile: ProfileCapability | undefined, parameter: string) {
  return Boolean(profile && Object.prototype.hasOwnProperty.call(profile.parameter_schema, parameter));
}

export function promptImageReferenceNumbers(prompt: string): number[] {
  const numbers: number[] = [];
  // Require an actual ordinal token. This intentionally does not interpret
  // phrases such as "image 4K" or "image 16:9" as connected references.
  const expressions = [
    /(?:图片|图)\s*#?\s*(\d+)(?!\s*[Kk:]|\d)/gu,
    /(?<![\w])(?:picture|image)\s*#?\s*(\d+)(?!\s*[Kk:]|[\w])/giu,
    /<(?:picture|image)\s*(\d+)>/giu,
  ];
  for (const expression of expressions) {
    for (const match of prompt.matchAll(expression)) numbers.push(Number(match[1]));
  }
  return [...new Set(numbers.filter((number) => Number.isInteger(number) && number > 0))].sort((a, b) => a - b);
}
