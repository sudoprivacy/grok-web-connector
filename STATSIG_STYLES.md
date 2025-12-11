# Statsig ID → Style Research Log

> Goal: Understand how `x-statsig-id` maps to video generation styles, and whether there are discrete style buckets or continuous variation.

---

## Experiment Log

### Exp #1 (2025-12-11)

**Statsig ID:** `W6IFgVSv2YSVxFj5Yt971KvAL1ldD75XJoGIR285iLdGPIiPNM7S1C9An8vmKsYbR9N5sF963w2iXoRhwSHYizPczaEUWA`

| Image | Video URL | Style Observed |
|-------|-----------|----------------|
| [9ac51419](https://grok.com/imagine/post/9ac51419-65c8-467c-958e-97e9f1abadfa) | same link | Camera: slow zoom in. Subject: almost no motion. |

**Observation:** Static subject + slow dolly forward.

---

## Open Questions

1. **Discrete vs Continuous?** Are there N fixed style buckets, or is it a continuous style space?
2. **ID Structure Matters?** Does the byte pattern affect style, or just the hash?
3. **Cross-Image Consistency?** Does same ID produce same style across very different images (portrait vs landscape, human vs object)?
4. **Style Dimensions:** What are the independent style axes? (camera motion, subject motion, timing, ...)

---

## Conclusions

*(To be updated as we collect more data)*

---

## Raw Data

```
ID: W6IFgVSv2YSVxFj5Yt971KvAL1ldD75XJoGIR285iLdGPIiPNM7S1C9An8vmKsYbR9N5sF963w2iXoRhwSHYizPczaEUWA
Style: static_subject + slow_zoom_in
Images tested: 1
```
