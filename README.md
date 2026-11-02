# reddit_needs

Agent qui repère sur Reddit les posts exprimant un besoin, les fait analyser par un LLM local (vLLM, API compatible OpenAI), puis classe les besoins pouvant être satisfaits par un moyen numérique.

## Pipeline

1. `collect` : récupère les posts candidats via l'API officielle de Reddit et les stocke dans SQLite.
2. `analyze` : analyse chaque candidat avec le LLM (besoin, intensité, volonté de payer, faisabilité).
3. `report` : génère `rapport_besoins.md` et `rapport_besoins.csv`.

## Prérequis

- Python 3.9+
- Un accès à l'API Reddit **approuvé par Reddit** (voir la [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)). Un usage commercial exige une approbation écrite explicite.
- Un serveur vLLM joignable.

## Installation

```
pip install -r requirements.txt
copy .env.example .env     # puis renseigner les valeurs
```

Le fichier `.env` contient les identifiants et n'est jamais versionné.

## Usage

```
python reddit_needs_agent.py collect
python reddit_needs_agent.py analyze
python reddit_needs_agent.py report
python reddit_needs_agent.py all
```

## Données

La base `needs.db` et les rapports contiennent des données issues de Reddit : ils sont exclus du dépôt (`.gitignore`) et ne doivent pas être redistribués.
