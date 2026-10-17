{% macro pub(p, abstract=True) -%}
- **{{ p.title | md_escape }}.** {{ p.authors_text }}{% if p.editors %} (eds.){% endif %}. {% if p.venue %}*{{ p.venue }}*, {% endif %}{{ p.year }}.{% if p.award %} **{{ p.award }}.**{% endif %}{% if p.links %} {% for l in p.links %}[{{ l.label }}]({{ base ~ l.url if l.url.startswith('/') else l.url }}){% if not loop.last %} · {% endif %}{% endfor %}{% endif %}

{% if abstract and p.abstract %}
  Abstract: {{ p.abstract }}

{% endif %}
{%- endmacro %}
