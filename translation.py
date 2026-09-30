"""Translate sentence continuations together and keep original cue boundaries."""
import hashlib
import json
import re


def sentence_groups(cues, enabled=True):
    groups, pending = [], []
    for cue in cues:
        if pending and (not enabled or len(pending) >= 3
                        or cue["start"] - pending[-1]["end"] > .8
                        or sum(len(c["english"]) for c in pending) + len(cue["english"]) > 240):
            groups.append(pending)
            pending = []
        pending.append(cue)
        if not enabled or re.search(r'[.!?]["\x27]?$' , cue["english"].strip()):
            groups.append(pending)
            pending = []
    if pending:
        groups.append(pending)
    return groups


def split_translation(text, group):
    words = text.split()
    if len(words) < len(group):
        return None  # Caller translates individual cues instead of emitting empty text.
    weights = [max(1, len(c["english"].split())) for c in group]
    total, offset, cumulative, parts = sum(weights), 0, 0, []
    for index, weight in enumerate(weights):
        cumulative += weight
        end = len(words) if index == len(group) - 1 else max(
            offset + 1, min(len(words) - (len(group) - index - 1), round(len(words) * cumulative / total)))
        parts.append(" ".join(words[offset:end]))
        offset = end
    return parts


def group_key(group, model_revision):
    identity = {"policy": 1, "model": model_revision,
                "source": [(c["id"], c["english"]) for c in group]}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
