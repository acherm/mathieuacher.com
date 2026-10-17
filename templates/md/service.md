# Students, projects & service: {{ site.person.name }}

Source: {{ base }}/service/ · Last updated: {{ built }}

{{ service.summary }}

{% for group in people %}
## {{ group.title }}

{% for s in group.members %}
- **{{ s.name }}**{% if s.years %} ({{ s.years }}{% if s.role %}, {{ s.role }}{% endif %}){% endif %}{% if s.topic %}: {{ s.topic }}{% endif %}{% if s.with %} With {{ s.with }}.{% endif %}{% if s.funding %} {{ s.funding }}.{% endif %}{% if s.note %} {{ s.note }}{% endif %}{% if s.now %} Now: {{ s.now }}.{% endif %}

{% endfor %}

{% endfor %}
## Research projects

{% for pr in projects %}
- **{{ pr.name }}**{% if pr.years or pr.role %} ({{ [pr.years, pr.role] | select | join(", ") }}){% endif %}: {{ pr.text }}{% if pr.url %} {{ pr.url }}{% endif %}

{% endfor %}

## Organization & leadership

{% for r in service.roles %}
- {{ r.year }}: {{ r.text }}
{% endfor %}

## Keynotes and invited talks

{% for y in talks %}
{% for t in y.talks %}
- {{ y.year }}: {{ t.label }}, {{ t.event }}{% if t.place %} ({{ t.place }}){% endif %}{% if t.title %}: *{{ t.title }}*{% endif %}{% if t.url %} {{ t.url }}{% endif %}

{% endfor %}
{% endfor %}

## Program committees

{{ service.committees }}

## Juries and prize committees

{% for g in juries.groups %}
- {{ g.label }}: {{ g.names | join(", ") }}
{% endfor %}
{% for o in juries.other %}
- {{ o }}
{% endfor %}
- Full record of French PhD juries: {{ juries.record }}

## Journal reviewing

{{ service.reviewing }}
