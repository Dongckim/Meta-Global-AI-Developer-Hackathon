
## 2026-10-31
- start: {'DWELL_S': 0.5, 'DWELL_MM': 4.5, 'MAX_MISSING': 2, 'EDGE_TOL': 0.01, 'FIT_TRIES': 200, 'MIN_EDGE_POINTS': 8, 'MAX_SKEW': 25.0}
- why: first baseline

## 2026-10-31
- DWELL_S 0.5 -> 1.0, MAX_VIEW_SKEW None -> 20.0, MAX_VIEW_RATIO None -> 1.35, ORB_FEATURES None -> 2000, MATCH_PITCHES None -> 1.0, MIN_INLIERS None -> 15, MIN_INLIER_SHARE None -> 0.3, MAX_BITS None -> 64, PLATE_MARGIN None -> 1.15, MIN_BASE_SIZE None -> 0.85, EDGE_GAP None -> 0.02, MIN_SHOWN None -> 0.9
- why: 실제 피펫팅은 팁이 웰 안에 1초 안팎 머묾, 책상 클립에서 맞춘 수 그대로·오탐 7→4. 플레이트 추적 값은 P1에서 처음 기록. 011204 회귀(4/4 → 3/4)는 승인: 이전 4/4는 찌그러진 격자의 우연한 결과로 추정

## 2026-10-31 (not yet a baseline)
- LOST_FRAMES None -> 3, LOST_REACH None -> 3.0
- why: following stayed lost on 230954 (115 frames left in place) with a hand over part of the plate; recovery added. Accuracy is checked by hand on the iPad.

## 2026-10-31 (not yet a baseline)
- FLAT_PX None -> 6, MAX_WELL_SHIFT None -> 4, MIN_WELLS None -> 30, SHIFT_MARGIN None -> 2, MAX_WELL_SCALE None -> 0.06
- why: seen from the side the mask's outline takes in the plate's wall and pushes the grid off the wells; the grid now fits the wells on the reference frame.

## 2026-10-31 (not yet a baseline)
- FLAT_PX 6 -> 12, MIN_WELLS 30 -> 12; ring peaks replaced by SAM "hole" on the flattened reference
- why: ring peaks fit only 4 of 18 clips on a clear plate, and two of those got worse.
