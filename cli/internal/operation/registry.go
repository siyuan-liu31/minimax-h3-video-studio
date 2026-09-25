package operation

import (
	"fmt"
	"math"
	"regexp"
	"sort"

	"h3studio/cli/internal/contract"
)

const OperationSchemaVersion = "h3ctl.operation/v1"

type Definition struct {
	Name        string
	InputSchema map[string]any
}

var definitions = buildDefinitions()

func Definitions() []Definition {
	names := make([]string, 0, len(definitions))
	for name := range definitions {
		names = append(names, name)
	}
	sort.Strings(names)
	result := make([]Definition, 0, len(names))
	for _, name := range names {
		result = append(result, definitions[name])
	}
	return result
}

func Schema(name string) (map[string]any, bool) {
	definition, ok := definitions[name]
	return definition.InputSchema, ok
}

func ValidateInput(name string, input map[string]any) error {
	definition, ok := definitions[name]
	if !ok {
		return contract.NewError("not_found", "operation not found")
	}
	if err := validateSchema(definition.InputSchema, input, "input"); err != nil {
		return err
	}
	switch name {
	case "media.frame":
		position, _ := input["position"].(string)
		_, hasTime := input["time"]
		if position == "current" && !hasTime {
			return usageError("input requires time for current frame")
		}
		if position != "current" && hasTime {
			return usageError("input.time is only valid for current frame")
		}
	case "media.trim":
		if input["end"].(float64) <= input["start"].(float64) {
			return usageError("input.end must be after input.start")
		}
	case "media.prepare_reference":
		short, hasShort := input["max_short_edge"].(float64)
		long, hasLong := input["max_long_edge"].(float64)
		if hasShort && hasLong && short > long {
			return usageError("input.max_short_edge cannot exceed input.max_long_edge")
		}
		if _, hasAudio := input["audio"]; !hasAudio && input["preset"] != "h3-low-token" {
			return usageError("input requires audio keep|remove unless preset is h3-low-token")
		}
	case "video.character_migration.plan", "video.character_migration.produce":
		segment := int(numberValueForValidation(input["segment_frames"], 243))
		overlap := int(numberValueForValidation(input["overlap_frames"], 39))
		if overlap >= segment {
			return usageError("input.overlap_frames must be smaller than input.segment_frames")
		}
	}
	return nil
}

func ValidateGenerationPayload(kind string, payload map[string]any) error {
	definition, ok := definitions["generate."+kind]
	if !ok {
		return contract.NewError("invalid_argument", "output_type must be image or video")
	}
	properties := definition.InputSchema["properties"].(map[string]any)
	for key, value := range payload {
		if rule, exists := properties[key].(map[string]any); exists {
			if err := validateSchema(rule, value, key); err != nil {
				return err
			}
		}
	}
	return nil
}

func buildDefinitions() map[string]Definition {
	stringRule := map[string]any{"type": "string", "minLength": 1}
	idRule := map[string]any{"type": "string", "pattern": "^[0-9a-f]{32}$"}
	boolRule := map[string]any{"type": "boolean"}
	numberRule := func(minimum float64) map[string]any { return map[string]any{"type": "number", "minimum": minimum} }
	integerRule := func(minimum float64) map[string]any { return map[string]any{"type": "integer", "minimum": minimum} }
	object := func(required []string, properties map[string]any) map[string]any {
		value := map[string]any{"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": false, "properties": properties}
		if len(required) > 0 {
			requiredJSON := make([]any, len(required))
			for index, field := range required {
				requiredJSON[index] = field
			}
			value["required"] = requiredJSON
		}
		return value
	}
	enum := func(values ...any) map[string]any { return map[string]any{"type": "string", "enum": values} }
	referenceProperties := func(maximumIndex float64) map[string]any {
		return map[string]any{
			"source":          stringRule,
			"asset_id":        idRule,
			"role":            enum("reference", "first", "last", "first_frame", "last_frame", "init_image", "image_edit", "identity", "style", "composition", "motion", "camera", "pacing", "voice", "music", "rhythm"),
			"reference_index": map[string]any{"type": "integer", "minimum": float64(0), "maximum": maximumIndex},
			"label":           map[string]any{"type": "string", "maxLength": float64(128)},
			"include_audio":   boolRule,
			"voice_speaker":   map[string]any{"type": "string"},
			"voice_subject":   map[string]any{"type": "integer", "minimum": float64(0)},
		}
	}
	references := func(maximum float64) map[string]any {
		referenceObject := func(required string) map[string]any {
			return object([]string{required}, referenceProperties(maximum-1))
		}
		return map[string]any{
			"type": "array", "maxItems": maximum,
			"items": map[string]any{"oneOf": []any{stringRule, referenceObject("source"), referenceObject("asset_id")}},
		}
	}
	commonParameters := map[string]any{
		"aspect_ratio":  enum("16:9", "9:16", "3:4", "1:1", "landscape", "portrait", "square", "1344x768", "768x1344", "1024x1024"),
		"resolution":    map[string]any{"type": "string", "minLength": float64(1)},
		"width":         map[string]any{"type": "integer", "minimum": float64(256), "maximum": float64(2048)},
		"height":        map[string]any{"type": "integer", "minimum": float64(256), "maximum": float64(2048)},
		"steps":         map[string]any{"type": "integer", "minimum": float64(1), "maximum": float64(100)},
		"seed":          map[string]any{"type": "integer", "minimum": float64(-1)},
		"denoise":       map[string]any{"type": "number", "minimum": float64(0.05), "maximum": float64(1)},
		"lora_strength": map[string]any{"type": "number", "minimum": float64(0), "maximum": float64(2)},
	}
	imageParameters := cloneProperties(commonParameters)
	imageParameters["width"] = map[string]any{"type": "integer", "minimum": float64(256), "maximum": float64(3072), "multipleOf": float64(8)}
	imageParameters["height"] = map[string]any{"type": "integer", "minimum": float64(256), "maximum": float64(3072), "multipleOf": float64(8)}
	imageParameters["cfg"] = map[string]any{"type": "number", "minimum": float64(1), "maximum": float64(30)}
	imageParameters["negative_prompt"] = map[string]any{"type": "string"}
	videoParameters := cloneProperties(commonParameters)
	videoParameters["aspect_ratio"] = enum("16:9", "9:16", "1:1", "landscape", "portrait", "square", "1344x768", "768x1344", "1024x1024")
	videoParameters["width"] = map[string]any{"type": "integer", "minimum": float64(256), "maximum": float64(2048), "multipleOf": float64(32)}
	videoParameters["height"] = map[string]any{"type": "integer", "minimum": float64(256), "maximum": float64(2048), "multipleOf": float64(32)}
	videoParameters["steps"] = map[string]any{"type": "integer", "minimum": float64(4), "maximum": float64(50)}
	videoParameters["duration"] = map[string]any{"type": "number", "minimum": float64(5), "maximum": float64(362) / 24}
	videoParameters["ref_image_size"] = enum("match", "max")
	videoParameters["mode"] = enum("auto", "text", "fl2va", "ref2va")
	videoParameters["director_mode"] = enum("auto", "t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v")
	videoParameters["source_asset_id"] = stringRule
	generateProperties := func(video bool) map[string]any {
		parameters := imageParameters
		maximumReferences := float64(10)
		if video {
			parameters = videoParameters
			maximumReferences = 12
		}
		value := map[string]any{
			"prompt":          stringRule,
			"profile_id":      map[string]any{"type": "string", "default": "auto"},
			"profile_version": map[string]any{"type": "string"},
			"profile_digest":  map[string]any{"type": "string"},
			"references":      references(maximumReferences),
			"parameters":      object(nil, parameters),
			"request_id":      idRule,
		}
		if video {
			value["prompt_mode"] = map[string]any{"type": "string", "enum": []any{"default", "preserve_tags_only"}, "default": "preserve_tags_only"}
			value["director_mode"] = enum("t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v")
			value["source_asset_id"] = stringRule
		} else {
			value["negative_prompt"] = map[string]any{"type": "string"}
		}
		return value
	}
	defs := map[string]Definition{}
	add := func(name string, required []string, properties map[string]any) {
		defs[name] = Definition{Name: name, InputSchema: object(required, properties)}
	}
	add("asset.upload", []string{"path"}, map[string]any{"path": stringRule, "kind": map[string]any{"type": "string", "enum": []any{"auto", "image", "video", "audio"}, "default": "auto"}})
	add("asset.download", []string{"asset_id", "to"}, map[string]any{"asset_id": idRule, "to": stringRule, "force": boolRule})
	add("asset.list", nil, map[string]any{"query": map[string]any{"type": "string"}, "folder_id": idRule})
	add("asset.copy", []string{"source", "to_context"}, map[string]any{"source": stringRule, "to_context": stringRule})
	add("generate.image", []string{"prompt"}, generateProperties(false))
	add("generate.video", []string{"prompt", "director_mode"}, generateProperties(true))
	add("job.list", nil, map[string]any{"limit": map[string]any{"type": "integer", "minimum": float64(1), "maximum": float64(100), "default": float64(20)}, "cursor": map[string]any{"type": "string"}, "results": boolRule})
	add("job.get", []string{"job_id"}, map[string]any{"job_id": idRule})
	add("job.wait", []string{"job_id"}, map[string]any{"job_id": idRule, "timeout_seconds": numberRule(0), "poll_seconds": numberRule(0)})
	add("job.cancel", []string{"job_id"}, map[string]any{"job_id": idRule})
	add("job.download", []string{"job_id", "to"}, map[string]any{"job_id": idRule, "index": integerRule(0), "to": stringRule, "force": boolRule})
	add("job.save", []string{"job_id"}, map[string]any{"job_id": idRule, "index": integerRule(0), "display_name": map[string]any{"type": "string"}, "folder_id": idRule})
	add("job.workflow", []string{"job_id"}, map[string]any{"job_id": idRule, "to": map[string]any{"type": "string"}, "force": boolRule})
	add("job.resume", []string{"job_id", "additional_steps"}, map[string]any{"job_id": idRule, "additional_steps": integerRule(1), "request_id": map[string]any{"type": "string", "minLength": float64(8)}, "wait": boolRule, "timeout_seconds": numberRule(0), "poll_seconds": numberRule(0), "download": map[string]any{"type": "string"}, "force": boolRule})
	add("job.delete", []string{"job_id"}, map[string]any{"job_id": idRule})
	add("media.frame", []string{"source", "position"}, map[string]any{"source": stringRule, "position": enum("first", "last", "current"), "time": numberRule(0), "display_name": map[string]any{"type": "string"}})
	add("media.endpoints", []string{"source"}, map[string]any{"source": stringRule})
	add("media.trim", []string{"source", "start", "end"}, map[string]any{"source": stringRule, "start": numberRule(0), "end": numberRule(0), "audio": boolRule, "display_name": map[string]any{"type": "string"}})
	add("media.extract_audio", []string{"source"}, map[string]any{"source": stringRule, "display_name": map[string]any{"type": "string"}})
	add("media.remove_audio", []string{"source"}, map[string]any{"source": stringRule, "display_name": map[string]any{"type": "string"}})
	add("media.mux_audio", []string{"video", "audio"}, map[string]any{
		"video": stringRule, "audio": stringRule, "duration": numberRule(0.001),
		"display_name": map[string]any{"type": "string"},
	})
	add("media.prepare_reference", []string{"source"}, map[string]any{
		"source": stringRule, "preset": enum("h3-low-token"),
		"max_short_edge": integerRule(32), "max_long_edge": integerRule(32),
		"fps":          map[string]any{"type": "integer", "enum": []any{float64(24)}},
		"max_duration": numberRule(0.001), "audio": enum("keep", "remove"),
		"fit": enum("contain"), "alignment": map[string]any{"type": "integer", "enum": []any{float64(32)}},
		"pad_mode": enum("edge"), "display_name": map[string]any{"type": "string"},
	})
	add("media.list", nil, map[string]any{})
	add("media.get", []string{"media_id"}, map[string]any{"media_id": idRule})
	add("media.download", []string{"media_id", "to"}, map[string]any{"media_id": idRule, "to": stringRule, "force": boolRule})
	add("media.save", []string{"media_id"}, map[string]any{"media_id": idRule, "display_name": map[string]any{"type": "string"}, "folder_id": idRule})
	add("media.delete", []string{"media_id"}, map[string]any{"media_id": idRule})
	add("douyin.submit", []string{"text"}, map[string]any{"text": stringRule, "mode": enum("parse", "download"), "quality": enum("best", "1080", "720"), "request_id": idRule})
	add("douyin.capabilities", nil, map[string]any{})
	add("douyin.list", nil, map[string]any{})
	for _, action := range []string{"get", "cancel", "retry"} {
		add("douyin."+action, []string{"task_id"}, map[string]any{"task_id": idRule})
	}
	add("douyin.wait", []string{"task_id"}, map[string]any{"task_id": idRule, "timeout_seconds": numberRule(0), "poll_seconds": numberRule(0)})
	add("voice.convert", []string{"engine", "source", "reference"}, map[string]any{
		"engine": enum("vevo2", "yingmusic"), "source": stringRule, "reference": stringRule,
		"diffusion_steps":    map[string]any{"type": "integer", "minimum": 10, "maximum": 200},
		"inference_cfg_rate": map[string]any{"type": "number", "minimum": 0, "maximum": 2},
		"seed":               map[string]any{"type": "integer", "minimum": -1, "maximum": 4294967295},
		"output_options": map[string]any{"type": "object", "additionalProperties": false,
			"properties": map[string]any{"include_stems": boolRule, "echo": boolRule, "reverb": boolRule}},
		"request_id": idRule, "wait": boolRule, "timeout_seconds": numberRule(0),
		"poll_seconds": numberRule(0), "download": map[string]any{"type": "string"}, "download_track": enum("mix", "dry_vocal", "accompaniment"), "force": boolRule,
	})

	add("voice.rewrite", []string{"source", "lyrics"}, map[string]any{
		"source": stringRule, "reference": stringRule, "preview": boolRule,
		"lyrics":             map[string]any{"type": "string", "minLength": 1, "maxLength": 10000},
		"original_lyrics":    map[string]any{"type": "string", "maxLength": 10000},
		"diffusion_steps":    map[string]any{"type": "integer", "minimum": 16, "maximum": 100},
		"inference_cfg_rate": map[string]any{"type": "number", "minimum": 0, "maximum": 10},
		"seed":               map[string]any{"type": "integer", "minimum": -1, "maximum": 4294967295},
		"request_id":         idRule, "wait": boolRule, "timeout_seconds": numberRule(0), "poll_seconds": numberRule(0),
		"download": stringRule, "download_track": enum("mix", "dry_vocal", "accompaniment"), "force": boolRule,
	})
	add("voice.transcribe", []string{"source"}, map[string]any{"source": stringRule, "request_id": idRule, "wait": boolRule, "timeout_seconds": numberRule(0), "poll_seconds": numberRule(0)})
	add("voice.remix", []string{"task_id"}, map[string]any{"task_id": idRule, "vocal_gain_db": map[string]any{"type": "number", "minimum": -18, "maximum": 12}, "accompaniment_gain_db": map[string]any{"type": "number", "minimum": -18, "maximum": 12}})
	add("voice.get", []string{"task_id"}, map[string]any{"task_id": idRule})
	add("voice.wait", []string{"task_id"}, map[string]any{"task_id": idRule, "timeout_seconds": numberRule(0), "poll_seconds": numberRule(0)})
	add("voice.cancel", []string{"task_id"}, map[string]any{"task_id": idRule})
	add("voice.delete", []string{"task_id"}, map[string]any{"task_id": idRule})
	add("voice.download", []string{"task_id", "to"}, map[string]any{"task_id": idRule, "to": stringRule, "track": enum("mix", "dry_vocal", "accompaniment", "remix"), "force": boolRule})
	add("gpu.status", nil, map[string]any{})
	projectID := map[string]any{"project_id": idRule}
	add("project.create", []string{"spec"}, map[string]any{"spec": map[string]any{"type": "object"}})
	add("project.apply", []string{"project_id", "spec"}, map[string]any{"project_id": idRule, "spec": map[string]any{"type": "object"}})
	add("project.list", nil, map[string]any{})
	add("project.get", []string{"project_id"}, projectID)
	add("project.delete", []string{"project_id"}, projectID)
	add("project.run", []string{"project_id"}, map[string]any{"project_id": idRule, "segment_ids": map[string]any{"type": "array", "items": idRule, "uniqueItems": true}})
	add("project.wait", []string{"project_id"}, map[string]any{"project_id": idRule, "timeout_seconds": numberRule(0), "poll_seconds": numberRule(0)})
	add("project.stop", []string{"project_id"}, projectID)
	add("project.rerun", []string{"project_id", "segment_id"}, map[string]any{"project_id": idRule, "segment_id": idRule})
	add("project.merge", []string{"project_id"}, projectID)
	add("project.download", []string{"project_id", "to"}, map[string]any{"project_id": idRule, "to": stringRule, "force": boolRule})
	add("video.compose", []string{"spec", "to"}, map[string]any{
		"spec": map[string]any{"type": "object"}, "to": stringRule, "force": boolRule,
		"timeout_seconds": numberRule(0), "poll_seconds": numberRule(0),
	})
	legalSegmentFrames := make([]any, 0, 15)
	for frames := 124; frames <= 362; frames += 17 {
		legalSegmentFrames = append(legalSegmentFrames, float64(frames))
	}
	migrationTarget := object([]string{"character", "source_subject"}, map[string]any{
		"character":      stringRule,
		"source_subject": map[string]any{"type": "string", "minLength": float64(3), "maxLength": float64(500)},
		"details":        map[string]any{"type": "string", "maxLength": float64(4000)},
	})
	migrationProperties := map[string]any{
		"version":         enum("h3.character-migration/v1"),
		"source":          stringRule,
		"targets":         map[string]any{"type": "array", "minItems": float64(1), "maxItems": float64(1), "items": migrationTarget},
		"profile_id":      map[string]any{"type": "string", "minLength": float64(1), "default": "minimax-h3-ref2va"},
		"profile_version": map[string]any{"type": "string"},
		"profile_digest":  map[string]any{"type": "string"},
		"steps":           map[string]any{"type": "integer", "minimum": float64(4), "maximum": float64(50)},
		"lora_strength":   map[string]any{"type": "number", "minimum": float64(0), "maximum": float64(2)},
		"seed":            map[string]any{"type": "integer", "minimum": float64(-1)},
		"segment_frames":  map[string]any{"type": "integer", "enum": legalSegmentFrames, "default": float64(243)},
		"overlap_frames":  map[string]any{"type": "integer", "enum": []any{float64(5), float64(22), float64(39), float64(56)}, "default": float64(39)},
		"audio_policy":    map[string]any{"type": "string", "enum": []any{"copy-source", "reference-source", "generate", "mute"}, "default": "copy-source"},
		"prompt":          map[string]any{"type": "string", "minLength": float64(1), "maxLength": float64(12000)},
		"title":           map[string]any{"type": "string", "minLength": float64(1), "maxLength": float64(200)},
	}
	add("video.character_migration.plan", []string{"version", "source", "targets"}, cloneProperties(migrationProperties))
	produceProperties := cloneProperties(migrationProperties)
	produceProperties["to"] = stringRule
	produceProperties["force"] = boolRule
	produceProperties["detach"] = boolRule
	produceProperties["timeout_seconds"] = numberRule(0)
	produceProperties["poll_seconds"] = numberRule(0)
	add("video.character_migration.produce", []string{"version", "source", "targets", "to"}, produceProperties)
	replicationReference := object([]string{"source", "role"}, map[string]any{
		"source": stringRule,
		"role":   map[string]any{"type": "string", "minLength": float64(1), "maxLength": float64(100)},
	})
	replicationReplace := object(nil, map[string]any{
		"subject":  map[string]any{"type": "string", "maxLength": float64(2000)},
		"product":  map[string]any{"type": "string", "maxLength": float64(2000)},
		"setting":  map[string]any{"type": "string", "maxLength": float64(2000)},
		"script":   map[string]any{"type": "string", "maxLength": float64(2000)},
		"language": map[string]any{"type": "string", "maxLength": float64(2000)},
		"style":    map[string]any{"type": "string", "maxLength": float64(2000)},
	})
	replicationProperties := map[string]any{
		"version":         enum("h3.replication/v1"),
		"source":          stringRule,
		"brief":           map[string]any{"type": "string", "minLength": float64(1), "maxLength": float64(4000)},
		"title":           map[string]any{"type": "string", "minLength": float64(1), "maxLength": float64(200)},
		"preserve":        map[string]any{"type": "array", "uniqueItems": true, "items": enum("timing", "motion", "camera", "composition", "environment", "lighting", "interactions")},
		"replace":         replicationReplace,
		"references":      map[string]any{"type": "array", "maxItems": float64(11), "items": replicationReference},
		"profile_id":      map[string]any{"type": "string", "minLength": float64(1), "default": "minimax-h3-ref2va"},
		"profile_version": map[string]any{"type": "string"},
		"profile_digest":  map[string]any{"type": "string"},
		"steps":           map[string]any{"type": "integer", "minimum": float64(4), "maximum": float64(50)},
		"lora_strength":   map[string]any{"type": "number", "minimum": float64(0), "maximum": float64(2)},
		"seed":            map[string]any{"type": "integer", "minimum": float64(-1)},
		"continuity":      enum("auto", "none", "motion_context"),
		"audio_policy":    enum("copy-source", "reference-source", "generate", "mute"),
		"cut_frames":      map[string]any{"type": "array", "maxItems": float64(200), "items": map[string]any{"type": "integer", "minimum": float64(1)}},
		"prompt":          map[string]any{"type": "string", "minLength": float64(1), "maxLength": float64(12000)},
	}
	add("video.replication.plan", []string{"version", "source", "brief"}, cloneProperties(replicationProperties))
	replicationProduce := cloneProperties(replicationProperties)
	replicationProduce["to"] = stringRule
	replicationProduce["force"] = boolRule
	replicationProduce["detach"] = boolRule
	replicationProduce["timeout_seconds"] = numberRule(0)
	replicationProduce["poll_seconds"] = numberRule(0)
	add("video.replication.produce", []string{"version", "source", "brief", "to"}, replicationProduce)
	add("video.replication.create", []string{"plan"}, map[string]any{"plan": map[string]any{"type": "object"}})
	add("video.replication.inspect", []string{"project_id"}, map[string]any{"project_id": idRule})
	add("video.replication.export", []string{"project_id"}, map[string]any{"project_id": idRule})
	add("video.replication.edit_segment", []string{"project_id", "segment_id", "expected_updated_at"}, map[string]any{
		"project_id": idRule, "segment_id": idRule, "expected_updated_at": numberRule(0),
		"prompt": map[string]any{"type": "string", "minLength": float64(1), "maxLength": float64(12000)},
		"steps":  map[string]any{"type": "integer", "minimum": float64(4), "maximum": float64(50)},
		"seed":   map[string]any{"type": "integer", "minimum": float64(-1)},
	})
	add("video.replication.resume", []string{"project_id", "to"}, map[string]any{
		"project_id": idRule, "to": stringRule, "force": boolRule, "timeout_seconds": numberRule(0), "poll_seconds": numberRule(0),
	})

	return defs
}

func cloneProperties(value map[string]any) map[string]any {
	result := make(map[string]any, len(value))
	for key, item := range value {
		result[key] = item
	}
	return result
}

func validateSchema(schema map[string]any, value any, path string) error {
	if choices, ok := schema["oneOf"].([]any); ok {
		matches := 0
		for _, raw := range choices {
			if validateSchema(raw.(map[string]any), value, path) == nil {
				matches++
			}
		}
		if matches != 1 {
			return usageError("%s must match exactly one allowed shape", path)
		}
		return nil
	}
	kind, _ := schema["type"].(string)
	switch kind {
	case "object":
		object, ok := value.(map[string]any)
		if !ok {
			return usageError("%s must be object", path)
		}
		properties, _ := schema["properties"].(map[string]any)
		if schema["additionalProperties"] == false {
			for key := range object {
				if _, exists := properties[key]; !exists {
					return usageError("%s does not accept %s", path, key)
				}
			}
		}
		for _, key := range stringSlice(schema["required"]) {
			if item, exists := object[key]; !exists || item == nil {
				return usageError("%s requires %s", path, key)
			}
		}
		for key, item := range object {
			if rule, exists := properties[key].(map[string]any); exists {
				if err := validateSchema(rule, item, path+"."+key); err != nil {
					return err
				}
			}
		}
	case "array":
		array, ok := value.([]any)
		if !ok {
			return usageError("%s must be array", path)
		}
		if maximum, ok := number(schema["maxItems"]); ok && float64(len(array)) > maximum {
			return usageError("%s must contain at most %g items", path, maximum)
		}
		if minimum, ok := number(schema["minItems"]); ok && float64(len(array)) < minimum {
			return usageError("%s must contain at least %g items", path, minimum)
		}
		if schema["uniqueItems"] == true {
			seen := map[string]bool{}
			for _, item := range array {
				key := fmt.Sprintf("%#v", item)
				if seen[key] {
					return usageError("%s items must be unique", path)
				}
				seen[key] = true
			}
		}
		if itemSchema, ok := schema["items"].(map[string]any); ok {
			for index, item := range array {
				if err := validateSchema(itemSchema, item, fmt.Sprintf("%s[%d]", path, index)); err != nil {
					return err
				}
			}
		}
	case "string":
		text, ok := value.(string)
		if !ok {
			return usageError("%s must be string", path)
		}
		if minimum, ok := number(schema["minLength"]); ok && float64(len(text)) < minimum {
			return usageError("%s cannot be empty", path)
		}
		if maximum, ok := number(schema["maxLength"]); ok && float64(len(text)) > maximum {
			return usageError("%s is too long", path)
		}
		if pattern, ok := schema["pattern"].(string); ok {
			matched, err := regexp.MatchString(pattern, text)
			if err != nil || !matched {
				return usageError("%s has an invalid identifier format", path)
			}
		}
	case "boolean":
		if _, ok := value.(bool); !ok {
			return usageError("%s must be boolean", path)
		}
	case "number", "integer":
		numeric, ok := value.(float64)
		if !ok || math.IsNaN(numeric) || math.IsInf(numeric, 0) || kind == "integer" && numeric != math.Trunc(numeric) {
			return usageError("%s must be %s", path, kind)
		}
		if minimum, ok := number(schema["minimum"]); ok && numeric < minimum {
			return usageError("%s must be at least %g", path, minimum)
		}
		if maximum, ok := number(schema["maximum"]); ok && numeric > maximum {
			return usageError("%s must be at most %g", path, maximum)
		}
		if multiple, ok := number(schema["multipleOf"]); ok && math.Mod(numeric, multiple) != 0 {
			return usageError("%s must be a multiple of %g", path, multiple)
		}
	}
	if choices, ok := schema["enum"].([]any); ok {
		for _, choice := range choices {
			if value == choice {
				return nil
			}
		}
		return usageError("%s has an unsupported value", path)
	}
	return nil
}

func numberValueForValidation(value any, fallback float64) float64 {
	if result, ok := value.(float64); ok {
		return result
	}
	return fallback
}

func number(value any) (float64, bool) {
	result, ok := value.(float64)
	return result, ok
}

func stringSlice(value any) []string {
	if direct, ok := value.([]string); ok {
		return direct
	}
	raw, _ := value.([]any)
	result := make([]string, 0, len(raw))
	for _, item := range raw {
		if text, ok := item.(string); ok {
			result = append(result, text)
		}
	}
	return result
}

func usageError(format string, args ...any) error {
	return &contract.CLIError{Code: "usage", Message: fmt.Sprintf(format, args...)}
}
