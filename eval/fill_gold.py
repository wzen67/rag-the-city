#!/usr/bin/env python3
"""Recompute gold numbers from CSVs and patch eval/questions.json.

Person C can run this after A lands DuckDB views — point --crime/--three11
at views exported as CSV, or keep the gzipped Analyze Boston extracts.
Does not call an LLM. Does not write SQL for the product engine.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CRIME = ROOT / "data" / "crime-incident-reports-august-2015-to-date-source-new-system.csv.gz"
DEFAULT_311 = ROOT / "data" / "311-service-requests.csv.gz"
DEFAULT_FOOD = ROOT / "data" / "food-establishment-inspections.csv.gz"
DEFAULT_OPEN = ROOT / "data" / "open-space.csv.gz"
QUESTIONS = ROOT / "eval" / "questions.json"


def _connect():
    import duckdb

    return duckdb.connect()


def _read(path: Path) -> str:
    return f"read_csv_auto('{path}', ignore_errors=true)"


def compute(crime: Path, three11: Path, food: Path, open_space: Path, property_csv: Path | None) -> dict:
    con = _connect()
    c, t, f, o = _read(crime), _read(three11), _read(food), _read(open_space)

    b2 = con.execute(
        f"""
        WITH x AS (
          SELECT YEAR, DISTRICT, upper(trim(OFFENSE_DESCRIPTION)) AS d
          FROM {c}
          WHERE DISTRICT='B2' AND YEAR=2024
        )
        SELECT
          count(*) AS naive_all,
          count(*) FILTER (
            WHERE d NOT LIKE '%SICK ASSIST%'
              AND d NOT LIKE 'INVESTIGATE PERSON%'
              AND d NOT LIKE 'INVESTIGATE PROPERTY%'
              AND d NOT LIKE '%SERVICE TO OTHER AGENCY%'
          ) AS prd_crime,
          count(*) FILTER (
            WHERE d LIKE '%ASSAULT%' OR d LIKE '%MURDER%' OR d LIKE '%RAPE%'
               OR d LIKE '%ROBBERY%' OR d LIKE '%HOMICIDE%'
          ) AS violent_kw,
          count(*) FILTER (WHERE d LIKE '%SICK ASSIST%') AS sick
        FROM x
        """
    ).fetchone()

    ucr = con.execute(
        f"""
        SELECT count(*) FILTER (
          WHERE UCR_PART IS NOT NULL AND trim(cast(UCR_PART as varchar)) <> ''
        )
        FROM {c} WHERE YEAR=2024
        """
    ).fetchone()[0]

    dor = con.execute(
        f"""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE trim(on_time)='ONTIME') AS ont
        FROM {t} WHERE neighborhood='Dorchester'
        """
    ).fetchone()

    jp = con.execute(
        f"""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE trim(on_time)='ONTIME') AS ont,
               median(date_diff('day', try_cast(open_dt as timestamp), try_cast(closed_dt as timestamp))) AS med
        FROM {t} WHERE neighborhood='Jamaica Plain'
        """
    ).fetchone()

    rox = con.execute(
        f"""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE trim(on_time)='ONTIME') AS ont,
               median(date_diff('day', try_cast(open_dt as timestamp), try_cast(closed_dt as timestamp))) AS med
        FROM {t} WHERE neighborhood='Roxbury'
        """
    ).fetchone()

    ab = con.execute(
        f"""
        SELECT neighborhood, count(*) AS n
        FROM {t}
        WHERE neighborhood IN ('Allston / Brighton','Allston','Brighton')
        GROUP BY 1
        """
    ).fetchall()
    ab_n = sum(r[1] for r in ab)
    ab_parts = {r[0]: r[1] for r in ab}

    eb_top = con.execute(
        f"""
        SELECT type, count(*) AS n FROM {t}
        WHERE neighborhood='East Boston'
        GROUP BY 1 ORDER BY n DESC LIMIT 4
        """
    ).fetchall()

    ranks = con.execute(
        f"""
        SELECT neighborhood,
               round(100.0 * count(*) FILTER (WHERE trim(on_time)='ONTIME') / count(*), 1) AS pct
        FROM {t}
        WHERE neighborhood IS NOT NULL AND neighborhood <> 'Boston'
        GROUP BY 1 HAVING count(*) >= 500
        ORDER BY pct DESC
        """
    ).fetchall()

    depts = con.execute(
        f"""
        SELECT department,
               round(100.0 * count(*) FILTER (WHERE trim(on_time)='ONTIME') / count(*), 1) AS pct
        FROM {t}
        GROUP BY 1 ORDER BY count(*) DESC
        """
    ).fetchall()
    dept_map = {r[0]: r[1] for r in depts}

    rodents = con.execute(
        f"""
        SELECT count(*) FROM {t}
        WHERE lower(type) LIKE '%rodent%' OR lower(case_title) LIKE '%rodent%'
           OR lower(type) LIKE '%rat%'
        """
    ).fetchone()[0]

    food_zip = con.execute(
        f"""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE viol_level='*') AS crit
        FROM {f}
        WHERE replace(cast(zip as varchar), ' ', '') IN ('02135','2135')
        """
    ).fetchone()

    parks = con.execute(
        f"""
        SELECT count(*) AS n, round(sum(try_cast(ACRES as double)), 1) AS acres
        FROM {o} WHERE DISTRICT='Jamaica Plain'
        """
    ).fetchone()

    prop = {}
    if property_csv and property_csv.exists():
        p = _read(property_csv)
        rows = con.execute(
            f"""
            SELECT lpad(cast(ZIP_CODE as varchar), 5, '0') AS zip,
                   median(try_cast(replace(cast(TOTAL_VALUE as varchar), ',', '') as double)) AS med
            FROM {p}
            WHERE lpad(cast(ZIP_CODE as varchar), 5, '0') IN ('02119','02130')
            GROUP BY 1
            """
        ).fetchall()
        prop = {r[0]: r[1] for r in rows}

    def pct(num, den):
        return round(100.0 * num / den, 1) if den else None

    return {
        "T01": {"value": int(b2[1]), "naive_value": int(b2[0]), "sick_assist": int(b2[3])},
        "R02": {"value": int(b2[2])},
        "T04": {"value": 0, "ucr_part_nonempty_2024": int(ucr or 0)},
        "R08": {"value": pct(dor[1], dor[0]), "numerator": int(dor[1]), "denominator": int(dor[0])},
        "R01": {"ontime_pct": pct(jp[1], jp[0]), "park_sites": int(parks[0]), "park_acres": float(parks[1] or 0)},
        "R05": {
            "value": pct(food_zip[1], food_zip[0]),
            "numerator": int(food_zip[1]),
            "denominator": int(food_zip[0]),
        },
        "R03": {"top1": eb_top[0][0], "top1_count": int(eb_top[0][1]), "value": [r[0] for r in eb_top]},
        "M01": {
            "highest": ranks[0][0],
            "highest_pct": float(ranks[0][1]),
            "lowest": ranks[-1][0],
            "lowest_pct": float(ranks[-1][1]),
        },
        "M05": {"value": int(ab_n), "parts": ab_parts},
        "M04": {"value": int(rodents), "year": 2026},
        "M03": {k: dept_map.get(k) for k in ("BTDT", "ISD", "INFO", "GEN_")},
        "M07": {"worst": ranks[-1][0], "worst_pct": float(ranks[-1][1])},
        "M02": {
            "zip_02119_median_assessed": prop.get("02119"),
            "zip_02130_median_assessed": prop.get("02130"),
            "roxbury_median_close_days": float(rox[2]) if rox[2] is not None else None,
            "jp_median_close_days": float(jp[2]) if jp[2] is not None else None,
            "ontime_roxbury": pct(rox[1], rox[0]),
            "ontime_jp": pct(jp[1], jp[0]),
        },
    }


def patch(qpath: Path, gold: dict) -> None:
    data = json.loads(qpath.read_text())
    by_id = {q["id"]: q for q in data["questions"]}

    def set_value(qid, **fields):
        g = by_id[qid]["gold"]
        for k, v in fields.items():
            if k == "value_rank":
                g["value"] = v
            elif k == "checks":
                g.setdefault("checks", {}).update(v)
            else:
                g[k] = v

    set_value("T01", value=gold["T01"]["value"], naive_value=gold["T01"]["naive_value"], sick_assist=gold["T01"]["sick_assist"])
    set_value("R02", value=gold["R02"]["value"])
    set_value("T04", value=gold["T04"]["value"], ucr_part_nonempty_2024=gold["T04"]["ucr_part_nonempty_2024"])
    set_value("R08", value=gold["R08"]["value"], numerator=gold["R08"]["numerator"], denominator=gold["R08"]["denominator"])
    set_value("R05", value=gold["R05"]["value"], numerator=gold["R05"]["numerator"], denominator=gold["R05"]["denominator"])
    set_value("R03", value=gold["R03"]["value"], top1=gold["R03"]["top1"], top1_count=gold["R03"]["top1_count"])
    set_value("M05", value=gold["M05"]["value"], parts=gold["M05"]["parts"])
    set_value("M04", value=gold["M04"]["value"])
    set_value("M01", value_rank=gold["M01"])
    m07 = dict(by_id["M07"]["gold"].get("value") or {})
    m07.update(gold["M07"])
    set_value("M07", value_rank=m07)
    set_value("M03", value_rank=gold["M03"])
    set_value("M02", value_rank=gold["M02"])
    set_value("R01", checks=gold["R01"])
    qpath.write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--crime", type=Path, default=DEFAULT_CRIME)
    p.add_argument("--three11", type=Path, default=DEFAULT_311)
    p.add_argument("--food", type=Path, default=DEFAULT_FOOD)
    p.add_argument("--open-space", type=Path, default=DEFAULT_OPEN)
    p.add_argument(
        "--property",
        type=Path,
        default=Path.home() / "Downloads" / "fy2026-property-assessment-data_rev.csv",
    )
    p.add_argument("--questions", type=Path, default=QUESTIONS)
    args = p.parse_args()
    gold = compute(args.crime, args.three11, args.food, args.open_space, args.property)
    patch(args.questions, gold)
    print(json.dumps(gold, indent=2, default=str))
    print(f"patched {args.questions}")


if __name__ == "__main__":
    main()
