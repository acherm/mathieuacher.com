{% from "md/_macros.md" import pub %}
# {{ site.person.name }}

{{ site.person.job_title }} · {{ site.person.affiliations | map(attribute="name") | join(" · ") }}

{{ site.person.distinction }}

Website: {{ base }}/ · Email: {{ site.person.email }} · Last updated: {{ built }}

{{ about }}

Profiles: {% for pr in site.profiles %}[{{ pr.name }}]({{ pr.url }}){% if not loop.last %} · {% endif %}{% endfor %}


## News

{% for n in news %}
- {{ n.date | month_year }}: {{ n.text }}
{% endfor %}

## Research

{{ research.intro }}

{% for t in research.themes %}
### {{ t.title }}

{{ t.text }}
{% if t.links %}

{% for l in t.links %}
- [{{ l.label }}]({{ l.url }})
{% endfor %}
{% endif %}

{% endfor %}
## Selected publications

Full list with abstracts: {{ base }}/publications.md · BibTeX: {{ base }}/acher.bib

{% for p in selected %}
{{ pub(p, abstract=False) }}
{% endfor %}

## Software

{% for s in software %}
- **[{{ s.name }}]({{ s.url }})**{% if s.status %} ({{ s.status }}){% endif %}: {{ s.text | replace("\n", " ") }}
{% endfor %}

## Teaching

{{ teaching }}

## Outreach

{{ media.intro }}

{% for m in media.item %}
- **{{ m.title }}** ({{ m.outlet }}, {{ m.date }}){% if m.text %}: {{ m.text }}{% endif %}{% if m.url %} {{ m.url }}{% endif %}

{% endfor %}

## Students, projects & service

{{ service.summary }}

Details: {{ base }}/service.md

## Theses

{% for t in [theses.hdr, theses.phd] %}
### {{ t.kind }} ({{ t.year }}): {{ t.title }}

{{ t.institution }}{% if t.supervisors %}. {{ t.supervisors }}{% endif %}

{% for l in t.links %}[{{ l.label }}]({{ l.url }}){% if not loop.last %} · {% endif %}{% endfor %}


Committee: {{ t.committee | join("; ") }}.

Abstract: {{ t.abstract }}

{% endfor %}
## Chess

{{ chess }}

## Contact

{{ site.site.contact }}
