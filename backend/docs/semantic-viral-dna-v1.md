# Semantic Viral DNA v1

Semantic Viral DNA is a global semantic interpretation of one video. It is extracted once per `provider:model`, prompt, contract, and text input combination, then persisted on the global `viral_dna` row. It has no Analysis, user, scan, private-business, or private-strategy dependency.

The draft 0007 was never applied or used to generate production v1 outputs (production remains at 0006). Therefore 0007 is corrected in place and the final contract deliberately retains the v1 name; no 0008 exists.

## Contract freeze

Semantic Viral DNA contract v1 is frozen by this release. Future incompatible changes to fields, enum values, null semantics, or their meaning must use a new contract version; v1 must not be silently redefined.

## Scope and exclusions

It classifies observable communication from caption and transcript only: hook (text, type, mechanism, target, audience specificity), subject and angle, audience psychology, emotional engine, content function and role, spoken/semantic format, narrative structure, awareness, proof/positioning, CTA, and commerce.

It does not receive or classify views, likes, comments, shares, favorites, engagement, outlier score, ranking, or follower count. Performance is joined later in SQL. It does not infer visual format (talking head, podcast, slideshow, screen recording, b-roll, text overlay, product visual), channel-wide niche, private audience data, creator intent, or truth of claims. Topic, subtopic, and audience identity are preserved for later versioned niche taxonomy without rereading transcripts.

## Fields and enums

Nullable text fields are `hook_text` (280), `topic` (120), `subtopic` (120), `angle_summary` (240), `pain`, `desire`, `fear`, `audience_identity`, `belief`, `objection`, `promise` (240 each), `reframe` (280), `emotional_arc` (200), and `cta_text` (280). Text is stripped and whitespace-normalized; markdown and lists are rejected.

| Field | Closed values |
| --- | --- |
| hook_type | question, bold_claim, problem, how_to, contrarian, story_open, list_open, warning, case_result, direct_command, news_update, none_unclear |
| hook_mechanism | curiosity_gap, pain_identification, self_identification, specificity, novelty, contradiction, fear_loss, aspiration, social_proof, authority, urgency, open_loop, challenge, surprise, none_unclear |
| hook_target | problem, desire, identity, outcome, mistake, belief, opportunity, news, story, none_unclear |
| audience_specificity | generic, broad_segment, niche, micro_niche, unclear |
| angle_type | pain, mistake, myth, how_to, opportunity, contrarian, case_study, personal_experience, trend_news, comparison, warning, aspiration, identity, process, other_unclear |
| emotion_primary / secondary | curiosity, fear, anger, frustration, hope, aspiration, validation, surprise, urgency, belonging, humor, sadness, confidence, desire, neutral_unclear |
| content_function_primary / secondary | educate, inform, inspire_motivate, entertain, relate_validate, provoke_challenge, document, demonstrate, social_proof, sell, other_unclear |
| content_role_primary / secondary | reach, positioning, nurture, community, conversion, unclear |
| content_format | how_to, list, story, opinion, explanation, problem_solution, case_study, testimonial, confession, q_and_a, myth_busting, warning, comparison, review_demo, commentary, reaction, demonstration, other_unclear |
| narrative_structure | problem_solution, symptom_cause_solution, story_lesson, claim_proof_action, question_answer, myth_reframe, case_result_method, before_after, warning_fix, opinion_argument, listicle, problem_agitation_solution, hook_value_cta, other_unclear |
| awareness_stage | unaware, problem_aware, solution_aware, offer_aware, most_aware, mixed_unclear |
| proof_type | none, personal_experience, client_result, case_study, numbers, demonstration, testimonial, authority_reference, social_proof, other_unclear |
| authority_mechanism | none_unclear, expertise, experience, results, process, teaching, contrarian_criterion, transparency |
| creator_positioning_signal | expert, relatable, aspirational, challenger, educator, documentarian, entertainer, curator, none_unclear |
| cta_type | follow, comment, save, share, dm, visit_profile, link_in_bio, subscribe, download, book_call, purchase, multiple, none, unclear |
| cta_secondary_type | same CTA values except multiple |
| commercial_intent | none, soft, direct, unclear |
| offer_integration | none, implicit, contextual, method_reference, case_study, lead_magnet, explicit_pitch, unclear |
| offer_type | none, service, consulting_coaching, course, digital_product, membership, software, physical_product, affiliate, event, real_estate, other, unknown |
| monetization_model | none, service, lead_generation, digital_product, subscription, ecommerce, affiliate, sponsorship, marketplace, advertising, other, unknown |

Secondary emotion/function/role/CTA is nullable and must differ from its primary. `educate` and direct commercial intent can coexist because function and commerce are distinct dimensions.

## Null semantics, provider independence, and idempotence

For free text, `null` means insufficient evidence or not applicable; text is never fabricated to avoid null. Enum `none` means an observable absence where the dimension allows it, `unknown` means an unidentifiable offer/model, and `unclear`/`none_unclear` means classification is not supported by the evidence.

`app.llm.semantic_contract` is the sole source of truth for fields, enums, nullability, and JSON Schema. Adapters may use that schema for structured output, but the provider-neutral core always validates again. v1 contains no real provider, SDK, HTTP request, API key, or external call.

Provenance stores `semantic_model` as `provider:model`, plus prompt version, extraction time, status, and input hash. `semantic_input_sha256` includes contract version, prompt version, provider, model, language, caption, and transcript. A completed row with the same identity is `unchanged` with zero provider calls; changing any of those values re-extracts the same global row.

## Analytics readiness

Every dimension is a normal column rather than a JSON blob. Future analytics can `GROUP BY` or filter `hook_type`, `hook_mechanism`, `angle_type`, `content_role_primary`, `content_function_primary`, `audience_specificity`, `awareness_stage`, `proof_type`, `commercial_intent`, and `offer_integration`, then join snapshots for views, engagement, outlier_score, share_rate, or favorite_rate. Those joins, aggregates, and indexes are intentionally not implemented in v1.
