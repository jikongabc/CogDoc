use regex::Regex;
use std::cmp::Ordering;
use std::collections::{BTreeSet, HashSet};
use std::sync::OnceLock;
use unicode_casefold::UnicodeCaseFold;

use crate::tokenizer::tokenize_mixed_text_core;

const REASON_WITHIN_BUDGET: &str = "within_budget";
const REASON_QUERY_SPAN: &str = "query_span";
const REASON_LONG_SENTENCE_WINDOW: &str = "long_sentence_window";
const REASON_FALLBACK_NO_TERMS: &str = "fallback_no_terms";
const REASON_FALLBACK_NO_MATCH: &str = "fallback_no_match";

static WORD_RE: OnceLock<Regex> = OnceLock::new();

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct TextSpan {
    start: usize,
    end: usize,
}

#[derive(Debug, PartialEq)]
pub struct Selection {
    pub start: usize,
    pub end: usize,
    pub score: f64,
    pub matched_terms: Vec<String>,
    pub reason: &'static str,
    pub fallback: bool,
}

type Quality = (usize, usize, usize);

struct CharText<'a> {
    text: &'a str,
    chars: Vec<char>,
    byte_offsets: Vec<usize>,
}

struct FoldedText {
    text: String,
    byte_offsets: Vec<usize>,
    original_indexes: Vec<usize>,
}

impl FoldedText {
    fn new(text: &str) -> Self {
        let mut folded = String::new();
        let mut original_indexes = Vec::new();
        for (original_index, character) in text.chars().enumerate() {
            for folded_character in character.case_fold() {
                folded.push(folded_character);
                original_indexes.push(original_index);
            }
        }
        let byte_offsets = folded
            .char_indices()
            .map(|(offset, _)| offset)
            .chain([folded.len()])
            .collect();
        Self {
            text: folded,
            byte_offsets,
            original_indexes,
        }
    }

    fn original_span(&self, byte_start: usize, byte_end: usize) -> Option<TextSpan> {
        let folded_start = self
            .byte_offsets
            .binary_search(&byte_start)
            .unwrap_or_else(|index| index);
        let folded_end = self
            .byte_offsets
            .binary_search(&byte_end)
            .unwrap_or_else(|index| index);
        if folded_start >= folded_end {
            return None;
        }
        Some(TextSpan {
            start: self.original_indexes[folded_start],
            end: self.original_indexes[folded_end - 1] + 1,
        })
    }
}

fn casefold(text: &str) -> String {
    text.case_fold().collect()
}

impl<'a> CharText<'a> {
    fn new(text: &'a str) -> Self {
        let mut chars = Vec::new();
        let mut byte_offsets = Vec::new();
        for (offset, character) in text.char_indices() {
            byte_offsets.push(offset);
            chars.push(character);
        }
        byte_offsets.push(text.len());
        Self {
            text,
            chars,
            byte_offsets,
        }
    }

    fn len(&self) -> usize {
        self.chars.len()
    }

    fn slice(&self, start: usize, end: usize) -> &'a str {
        &self.text[self.byte_offsets[start]..self.byte_offsets[end]]
    }

    fn char_index_for_byte(&self, byte: usize) -> usize {
        self.byte_offsets
            .binary_search(&byte)
            .unwrap_or_else(|index| index)
    }
}

fn word_re() -> &'static Regex {
    WORD_RE.get_or_init(|| Regex::new(r"[A-Za-z0-9]+(?:[_.-][A-Za-z0-9]+)*").unwrap())
}

fn stable_tokens(text: &str) -> HashSet<String> {
    tokenize_mixed_text_core(text)
        .into_iter()
        .map(|token| casefold(token.trim()))
        .filter(|token| !token.is_empty())
        .collect()
}

fn scoring_tokens(text: &str, target_terms: &[String]) -> HashSet<String> {
    let mut tokens = stable_tokens(text);
    let folded = casefold(text);
    let target: HashSet<&str> = target_terms.iter().map(String::as_str).collect();
    for term in target_terms {
        let pure_ascii_alpha = term.is_ascii() && term.chars().all(|ch| ch.is_ascii_alphabetic());
        if !term.is_empty() && folded.contains(term) && !pure_ascii_alpha {
            tokens.insert(term.clone());
        }
    }
    for matched in word_re().find_iter(text) {
        let word = matched.as_str();
        let mut lexical = stable_tokens(word);
        lexical.insert(casefold(word));
        for term in lexical {
            if target.contains(term.as_str()) {
                tokens.insert(term);
            }
        }
    }
    tokens
}

fn quality(
    tokens: &HashSet<String>,
    query_terms: &[String],
    requirement_terms: &[Vec<String>],
    all_terms: &[String],
) -> Quality {
    let requirement_hits = requirement_terms
        .iter()
        .filter(|terms| terms.iter().any(|term| tokens.contains(term)))
        .count();
    let query_hits = query_terms
        .iter()
        .filter(|term| tokens.contains(*term))
        .count();
    let distinct_hits = all_terms
        .iter()
        .filter(|term| tokens.contains(*term))
        .count();
    (requirement_hits, query_hits, distinct_hits)
}

fn score(value: Quality) -> f64 {
    (value.0 * 100 + value.1 * 10 + value.2) as f64
}

fn matched_terms(tokens: &HashSet<String>, all_terms: &[String]) -> Vec<String> {
    all_terms
        .iter()
        .filter(|term| tokens.contains(*term))
        .cloned()
        .collect()
}

fn is_closing_punctuation(character: char) -> bool {
    matches!(
        character,
        '"' | '\''
            | '\u{2019}'
            | '\u{201d}'
            | ')'
            | '\u{ff09}'
            | ']'
            | '\u{3011}'
            | '}'
            | '\u{3009}'
            | '\u{300d}'
            | '\u{300f}'
    )
}

fn is_sentence_period(chars: &[char], index: usize) -> bool {
    if chars[index] != '.' {
        return false;
    }
    let previous = index.checked_sub(1).and_then(|pos| chars.get(pos)).copied();
    let following = chars.get(index + 1).copied();
    if previous.is_some_and(|ch| ch.is_ascii_digit())
        && following.is_some_and(|ch| ch.is_ascii_digit())
    {
        return false;
    }
    following.is_none_or(|ch| ch.is_whitespace() || is_closing_punctuation(ch))
}

fn trimmed_span(chars: &[char], mut start: usize, mut end: usize) -> Option<TextSpan> {
    while start < end && chars[start].is_whitespace() {
        start += 1;
    }
    while end > start && chars[end - 1].is_whitespace() {
        end -= 1;
    }
    (start < end).then_some(TextSpan { start, end })
}

fn sentence_spans(view: &CharText<'_>) -> Vec<TextSpan> {
    let mut spans = Vec::new();
    let mut start = 0;
    let mut index = 0;
    while index < view.len() {
        let character = view.chars[index];
        let boundary = character == '\n'
            || matches!(
                character,
                '\u{3002}' | '\u{ff01}' | '\u{ff1f}' | '!' | '?' | '\u{ff1b}' | ';'
            )
            || is_sentence_period(&view.chars, index);
        if !boundary {
            index += 1;
            continue;
        }
        let mut end = if character == '\n' { index } else { index + 1 };
        if character != '\n' {
            while end < view.len() && is_closing_punctuation(view.chars[end]) {
                end += 1;
            }
        }
        if let Some(span) = trimmed_span(&view.chars, start, end) {
            spans.push(span);
        }
        index = std::cmp::max(index + 1, end);
        start = index;
    }
    if let Some(span) = trimmed_span(&view.chars, start, view.len()) {
        spans.push(span);
    }
    spans
}

fn matching_intervals(view: &CharText<'_>, terms: &HashSet<String>) -> Vec<TextSpan> {
    let mut intervals = BTreeSet::new();
    let folded = FoldedText::new(view.text);
    for term in terms {
        if term.is_ascii() && term.chars().all(|ch| ch.is_ascii_alphabetic()) {
            continue;
        }
        let folded_term = casefold(term);
        for (byte_start, _) in folded.text.match_indices(&folded_term) {
            if let Some(span) = folded.original_span(byte_start, byte_start + folded_term.len()) {
                intervals.insert((span.start, span.end));
            }
        }
    }
    for matched in word_re().find_iter(view.text) {
        let mut lexical = stable_tokens(matched.as_str());
        lexical.insert(casefold(matched.as_str()));
        if lexical.iter().any(|term| terms.contains(term)) {
            intervals.insert((
                view.char_index_for_byte(matched.start()),
                view.char_index_for_byte(matched.end()),
            ));
        }
    }
    intervals
        .into_iter()
        .map(|(start, end)| TextSpan { start, end })
        .collect()
}

fn window_around_interval(text_chars: usize, interval: TextSpan, max_chars: usize) -> TextSpan {
    let interval_chars = interval.end - interval.start;
    let mut start = if interval_chars >= max_chars {
        (interval.start + interval.end) / 2 - max_chars / 2
    } else {
        interval
            .start
            .saturating_sub((max_chars - interval_chars) / 2)
    };
    start = std::cmp::min(start, text_chars.saturating_sub(max_chars));
    TextSpan {
        start,
        end: std::cmp::min(text_chars, start + max_chars),
    }
}

fn select_long_sentence_window(
    text: &str,
    target_terms: &[String],
    query_terms: &[String],
    requirement_terms: &[Vec<String>],
    max_chars: usize,
) -> Option<Selection> {
    let view = CharText::new(text);
    let targets: HashSet<String> = target_terms.iter().cloned().collect();
    let mut best: Option<(Quality, TextSpan, HashSet<String>)> = None;
    for interval in matching_intervals(&view, &targets) {
        let span = window_around_interval(view.len(), interval, max_chars);
        let tokens = scoring_tokens(view.slice(span.start, span.end), target_terms);
        let candidate_quality = quality(&tokens, query_terms, requirement_terms, target_terms);
        let replace = best.as_ref().is_none_or(|(best_quality, best_span, _)| {
            candidate_quality > *best_quality
                || (candidate_quality == *best_quality && span.start < best_span.start)
        });
        if replace {
            best = Some((candidate_quality, span, tokens));
        }
    }
    best.map(|(best_quality, span, tokens)| Selection {
        start: span.start,
        end: span.end,
        score: score(best_quality),
        matched_terms: matched_terms(&tokens, target_terms),
        reason: REASON_LONG_SENTENCE_WINDOW,
        fallback: false,
    })
}

fn compare_focal(
    left_quality: Quality,
    left_index: usize,
    right_quality: Quality,
    right_index: usize,
) -> Ordering {
    left_quality
        .cmp(&right_quality)
        .then_with(|| right_index.cmp(&left_index))
}

pub fn select_evidence_span_core(
    text: &str,
    query_terms: &[String],
    requirement_terms: &[Vec<String>],
    target_terms: &[String],
    max_chars: usize,
    context_sentences: usize,
) -> Selection {
    let view = CharText::new(text);
    let full_tokens = scoring_tokens(text, target_terms);
    let full_quality = quality(&full_tokens, query_terms, requirement_terms, target_terms);
    let full_matches = matched_terms(&full_tokens, target_terms);
    if view.len() <= max_chars {
        return Selection {
            start: 0,
            end: view.len(),
            score: score(full_quality),
            matched_terms: full_matches,
            reason: REASON_WITHIN_BUDGET,
            fallback: false,
        };
    }
    if target_terms.is_empty() {
        return Selection {
            start: 0,
            end: view.len(),
            score: 0.0,
            matched_terms: Vec::new(),
            reason: REASON_FALLBACK_NO_TERMS,
            fallback: true,
        };
    }
    if full_matches.is_empty() {
        return Selection {
            start: 0,
            end: view.len(),
            score: 0.0,
            matched_terms: Vec::new(),
            reason: REASON_FALLBACK_NO_MATCH,
            fallback: true,
        };
    }

    let sentences = sentence_spans(&view);
    let sentence_tokens: Vec<HashSet<String>> = sentences
        .iter()
        .map(|span| scoring_tokens(view.slice(span.start, span.end), target_terms))
        .collect();
    let target_set: HashSet<&str> = target_terms.iter().map(String::as_str).collect();
    let matching_indexes: Vec<usize> = sentence_tokens
        .iter()
        .enumerate()
        .filter(|(_, tokens)| {
            tokens
                .iter()
                .any(|token| target_set.contains(token.as_str()))
        })
        .map(|(index, _)| index)
        .collect();
    let Some(focal_index) = matching_indexes.into_iter().max_by(|left, right| {
        compare_focal(
            quality(
                &sentence_tokens[*left],
                query_terms,
                requirement_terms,
                target_terms,
            ),
            *left,
            quality(
                &sentence_tokens[*right],
                query_terms,
                requirement_terms,
                target_terms,
            ),
            *right,
        )
    }) else {
        return select_long_sentence_window(
            text,
            target_terms,
            query_terms,
            requirement_terms,
            max_chars,
        )
        .unwrap_or(Selection {
            start: 0,
            end: view.len(),
            score: 0.0,
            matched_terms: Vec::new(),
            reason: REASON_FALLBACK_NO_MATCH,
            fallback: true,
        });
    };
    let focal = sentences[focal_index];
    if focal.end - focal.start > max_chars {
        return select_long_sentence_window(
            view.slice(focal.start, focal.end),
            target_terms,
            query_terms,
            requirement_terms,
            max_chars,
        )
        .map(|mut selection| {
            selection.start += focal.start;
            selection.end += focal.start;
            selection
        })
        .unwrap_or(Selection {
            start: 0,
            end: view.len(),
            score: 0.0,
            matched_terms: Vec::new(),
            reason: REASON_FALLBACK_NO_MATCH,
            fallback: true,
        });
    }

    let lower = focal_index.saturating_sub(context_sentences);
    let upper = std::cmp::min(sentences.len() - 1, focal_index + context_sentences);
    let mut best: Option<(Quality, usize, usize, TextSpan, HashSet<String>)> = None;
    for start_index in lower..=focal_index {
        for end_index in focal_index..=upper {
            let span = TextSpan {
                start: sentences[start_index].start,
                end: sentences[end_index].end,
            };
            if span.end - span.start > max_chars {
                continue;
            }
            let tokens = scoring_tokens(view.slice(span.start, span.end), target_terms);
            let candidate_quality = quality(&tokens, query_terms, requirement_terms, target_terms);
            let candidate_key = (
                candidate_quality,
                end_index - start_index + 1,
                usize::MAX - (focal_index - start_index).abs_diff(end_index - focal_index),
                usize::MAX - start_index,
            );
            let replace = best
                .as_ref()
                .is_none_or(|(best_quality, best_start, best_end, _, _)| {
                    let best_key = (
                        *best_quality,
                        *best_end - *best_start + 1,
                        usize::MAX - (focal_index - *best_start).abs_diff(*best_end - focal_index),
                        usize::MAX - *best_start,
                    );
                    candidate_key > best_key
                });
            if replace {
                best = Some((candidate_quality, start_index, end_index, span, tokens));
            }
        }
    }
    let (best_quality, _, _, span, tokens) = best.expect("the focal sentence fits max_chars");
    Selection {
        start: span.start,
        end: span.end,
        score: score(best_quality),
        matched_terms: matched_terms(&tokens, target_terms),
        reason: REASON_QUERY_SPAN,
        fallback: false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selects_exact_english_sentence() {
        let text = "Intro sentence. The retrieval system uses hybrid search. Closing note.";
        let selection = select_evidence_span_core(
            text,
            &["hybrid".into(), "retriev".into()],
            &[],
            &["hybrid".into(), "retriev".into()],
            40,
            0,
        );
        let view = CharText::new(text);
        assert_eq!(
            view.slice(selection.start, selection.end),
            "The retrieval system uses hybrid search."
        );
        assert_eq!(selection.reason, REASON_QUERY_SPAN);
    }

    #[test]
    fn uses_character_offsets_for_chinese() {
        let text = "背景信息。系统采用向量检索提升召回率。部署成本。";
        let selection = select_evidence_span_core(
            text,
            &["向量".into(), "检索".into()],
            &[],
            &["向量".into(), "检索".into()],
            15,
            0,
        );
        let view = CharText::new(text);
        assert_eq!(
            view.slice(selection.start, selection.end),
            "系统采用向量检索提升召回率。"
        );
    }

    #[test]
    fn full_casefold_matches_python_semantics_and_maps_expansions() {
        assert_eq!(casefold("Straße İ Σς"), "strasse i\u{307} σσ");

        let folded = FoldedText::new("AİB");
        let folded_term = "i\u{307}";
        let byte_start = folded.text.find(folded_term).unwrap();
        assert_eq!(
            folded.original_span(byte_start, byte_start + folded_term.len()),
            Some(TextSpan { start: 1, end: 2 })
        );
    }
}
