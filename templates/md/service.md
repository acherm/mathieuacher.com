# Students, projects & service: {{ site.person.name }}

Source: {{ base }}/service/ · Last updated: {{ built }}

{{ service.summary }}

{% for group in service.people %}
## {{ group.title }}

{% for s in group.members %}
- **{{ s.name }}**{% if s.years %} ({{ s.years }}{% if s.role %}, {{ s.role }}{% endif %}){% endif %}{% if s.topic %}: {{ s.topic }}{% endif %}{% if s.with %} With {{ s.with }}.{% endif %}{% if s.funding %} {{ s.funding }}.{% endif %}{% if s.now %} Now: {{ s.now }}.{% endif %}

{% endfor %}

{% endfor %}
## Research projects

{% for pr in service.projects %}
- **{{ pr.name }}**{% if pr.years or pr.role %} ({{ [pr.years, pr.role] | select | join(", ") }}){% endif %}: {{ pr.text }}{% if pr.url %} {{ pr.url }}{% endif %}

{% endfor %}

## Organization & leadership

{% for r in service.roles %}
- {{ r.year }}: {{ r.text }}
{% endfor %}

## Program committees

{{ service.committees }}

## PhD juries and prize committees

{{ service.juries }}

## Journal reviewing

{{ service.reviewing }}
