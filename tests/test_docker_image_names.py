"""Podman (Fedora / 42 computers) refuses short image names without a TTY: we always pass full names."""

from agent_smith.docker_env import DockerEnv, qualify_image


def test_short_names_get_docker_hub():
    assert qualify_image("swebench/sweb.eval.x86_64.sympy_1776_sympy-13480:latest") == \
        "docker.io/swebench/sweb.eval.x86_64.sympy_1776_sympy-13480:latest"
    assert qualify_image("python:3.11-slim") == "docker.io/library/python:3.11-slim"


def test_full_names_unchanged():
    for name in ("docker.io/acme/a:b", "ghcr.io/x/y:1", "localhost/x:1", "localhost:5000/x"):
        assert qualify_image(name) == name


def test_docker_env_uses_full_name_and_keeps_platform():
    env = DockerEnv("swebench/sweb.eval.x86_64.django_1776_django-11066:latest", "django__django-11066")
    assert env.image == "docker.io/swebench/sweb.eval.x86_64.django_1776_django-11066:latest"
    assert env.platform == "linux/amd64"
