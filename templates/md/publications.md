{% from "md/_macros.md" import pub %}
# Publications of {{ site.person.name }}

Journal articles, conference and workshop papers, books, and other works ({{ years[-1] }}–{{ years[0] }}), newest first.

Source: {{ base }}/publications/ · BibTeX: {{ base }}/acher.bib · Last updated: {{ built }}

{% for pr in site.profiles if pr.scholarly %}{{ pr.name }}: {{ pr.url }}{% if not loop.last %} · {% endif %}{% endfor %}


{% if awards %}
## Awards

{% for p in awards %}
- {{ p.award }}: "{{ p.title }}", {{ p.venue }}, {{ p.year }}.
{% endfor %}

{% endif %}
{% for y in years %}
## {{ y }}

{% for p in pubs if p.year == y %}
{{ pub(p) }}
{% endfor %}
{% endfor %}
